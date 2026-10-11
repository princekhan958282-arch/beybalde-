# Avatar progression V4

Use `;ainfo` for your equipped card, or `;ainfo <name or id>` for another card. The existing template remains unchanged. The image and accompanying details show category, 1–15 stars, level, skill levels, permanent star gain, typed level allocation, total avatar stat contributions, feeding progress and the next upgrade probability/warning. Level Up, Skill Level Up, Star Up, Feed Stage, Details and Skills are owner-only controls. Changes refresh the image/details; destruction removes the image and disables every control.

## Approved balance

Settings live in `cogs/avatar/avatar_config.py`.

| Target | Required feeding copies | Success | Additional ATK, DEF and STM each |
|---|---:|---:|---:|
| 2★ | 5 | 100% | 5 |
| 3★ | 10 | 100% | 7 |
| 4★ | 15 | 100% | 10 |
| 5★ | 20 | 100% | 15 |
| 6★ | 25 | 50% | 40 |
| 7★ | 30 | 25% | 65 |
| 8★ | 35 | 5% | 110 |
| 9★ | 40 | Pending | 140 |
| 10★ | 45 | Pending | 175 |
| 11★ | 50 | Pending | 210 |
| 12★ | 55 | Pending | 250 |
| 13★ | 60 | Pending | 300 |
| 14★ | 65 | Pending | 350 |
| 15★ | 70 | Pending | 450 |

Star gains are cumulative: 8★ adds 252 to each stat; 15★ adds 2,127. Upgrade and feeding controls for unapproved target probabilities are locked. Stored higher stars remain readable and retain their approved stat bonuses.

Each level, **including Level 1**, allocates exactly 15 points: Attack 9/3/3, Defense 3/9/3, Stamina 3/3/9, Balance 5/5/5 (ATK/DEF/STM). Level 5 allocates 75 total. Existing level costs, skill maximum 10, card-level skill caps, triggers, energy costs, cooldowns and Original Generation abilities remain intact. The shared battle snapshot derives level and star bonuses once; PvP, Story and Boss use that pipeline.

## Feeding and risky upgrades

Category uses an explicit avatar `category` when present, otherwise the existing roster `rarity`/banner (e.g. Original Generation or Blader). Feed Stage opens a paginated list of eligible duplicate card definitions; selecting one opens a quantity modal. Only spare copies can be spent. Primary owned, equipped and upgraded cards are never materials; their independent untrained duplicate copies are eligible.

Submitting feeding consumes those selected copies and saves credits toward the next star. Star Up requires full feeding progress and charges no second copy fee or coins. Successful upgrades reset progress for the next star. A 30-second confirmation warns of permanent loss for risky targets. Cancelling or timing out changes nothing, including previously saved feeding credits. A failed risky upgrade removes the primary card, its progression and selected skill, and unequips it. Fed copies are never refunded; unused spare copies remain. Keeping a later reward can acquire a fresh Level 1, 1★ card. A saved incarnation token prevents an old confirmation from operating on this replacement.

## Keep or Sell

Packs, redeem codes, School League rewards and admin grants enqueue individual durable decisions. The compatibility inventory reward API also queues a decision rather than granting or selling directly. Only the receiving player can use KEEP AVATAR or SELL AVATAR.

Keep grants a primary card or adds one duplicate feeding copy and pays no refund. Sell pays only the newly awarded card's saved sell value and never touches existing cards. Pack sell values retain the previous pack-price/rarity refund amounts, including zero for Original Generation. Non-pack rewards use an authored `sell_value`/`sell_price`, defaulting to zero when none exists; additional non-pack prices require a balance decision.

Each reward has an independent ID and claim state. Timeout defaults to Keep after 180 seconds, with the recovery worker checking every 30 seconds. The worker registers saved message controls after startup, delivers undelivered decisions to the saved reward channel or the recipient's DM, and settles expired decisions. `;avatarrewards` recovers pending controls when a message is unavailable. Failed Discord delivery never loses the stored reward. Pack payment, cooldown and pending rewards commit together; command retries and repeated reward decisions cannot pay twice.

## Persistence and migration

Ownership, duplicates, progression, feeding credits and pending/resolved reward receipts stay in the existing SQLite/MySQL JSON profile row. SQLite `BEGIN IMMEDIATE` and MySQL InnoDB row locks serialize the entire read/modify/write, with rollback on validation or persistence failure. Reward settlement, currency payout and claim state are one transaction.

Migration preserves legacy ownership and equipped cards, existing levels/skills/stars/spending and duplicate IDs. Old V3 stages become feeding credits once because they represented already consumed copies. Level bonuses are calculated from stored level, never incrementally written; migration therefore cannot duplicate the first allocation. An existing inventory key, even an empty inventory after loss, prevents legacy reimport.

Avatar-changing transactions advance a revision. Generic stale profile saves are rejected so old snapshots cannot restore destroyed cards, pending decisions or old currency balances. Callers receiving a stale-save error must reload and reapply their intended mutation. Administrative snapshot restore remains an explicit recovery operation.

## Verification

- `python -m unittest tools.test_avatar_progression tools.test_avatar_progression_v4`
- `python -m unittest tools.test_original_generation tools.test_avatar_battle_connection tools.test_special_avatar_pipeline tools.sim_avatar_info_card`
- `python tools/sim_avatar.py`
- Existing component, startup and boss regressions.
- `python -m compileall -q cogs utils app.py`

Tests cover every configured tier and allocation, success boundaries, destruction, cancellation/timeout, same-category material selection, persistence, concurrent independent SQLite connections, stale confirmations/saves, all progression buttons, renders, rewards, recovery and real PvP/Story/Boss snapshots. Original Generation tests cover unique abilities and temporary skill-level empowerment.

CI provisions an isolated MySQL 8 service and runs the same transaction tests through `AVATAR_V4_TEST_MYSQL_URL`. Without that test-only URL, five live MySQL tests skip; mocked MySQL row-lock/commit/rollback checks still run. No production credentials or live Discord login are required. Live Discord interactions and the CI MySQL result must be checked before rollout.

## Remaining balance decisions

1. Success probabilities for target stars 9–15; no probabilities have been invented.
2. Non-pack sell prices for cards without an authored sell value. They currently sell for zero.

## Per-Bey avatar bonus limits

Permanent avatar ATK, DEF, STM and HP bonuses add flat gains plus a percentage of the stat before avatar bonuses. Each avatar has its own `stat_bonus_cap` in its JSON definition; 100% is the hard maximum, not a shared cap. Each combined bonus is capped at that avatar’s percentage of the Bey stat. HP uses the starting battle HP pool after Bey levels and parts are resolved, before avatar bonuses and type passives. Card and battle calculations read the same per-avatar cap; `AVATAR_STAT_BONUS_CAP` enforces the hard maximum. Stored stars and purchased levels are preserved; excess bonuses are capped only when applied. Skill upgrades scale magnitudes without changing hit intervals or effect durations.

Individual caps reserve less permanent stat growth for kits with strong crit, Special amplification, control, damage resistance or survival effects. Stat-focused kits receive more room. These are authored balance settings, not runtime formulas based on rarity. The cap covers permanent ATK/DEF/STM/HP bonuses; timed skill effects and Special damage keep their existing mechanics.

| Avatar | Combined stat bonus cap |
|---|---:|
| Abyssal Warden | 70% |
| Alya | 45% |
| Argus | 80% |
| Cobra Titan | 100% |
| Daichi | 95% |
| Daigo Kurogami | 80% |
| Dr. W. D. Gaster | 80% |
| Dyrroth | 80% |
| Eternal Inferno | 65% |
| Eudora | 85% |
| Freya | 85% |
| Gary | 100% |
| Gen | 50% |
| Helcurt | 80% |
| Hinata | 45% |
| Historia | 60% |
| Hoji Konda | 85% |
| Kai | 90% |
| Kanna | 40% |
| Ken Midori | 85% |
| Kevin | 95% |
| Lee | 100% |
| Mare | 100% |
| Mariah | 100% |
| Marina | 50% |
| Max | 90% |
| Miffy | 95% |
| Mikasa | 80% |
| Miku | 55% |
| Mitsuha | 60% |
| Mitsuri | 75% |
| Okudera | 45% |
| Omega Prime | 75% |
| Orochi Ginba | 80% |
| Rantaro Kiyama | 85% |
| Ray | 95% |
| Reina | 55% |
| Sable | 50% |
| Shadow Ninja | 75% |
| Shinobu | 70% |
| Shu Kurenai | 75% |
| Storm Reaper | 65% |
| Sukuna | 70% |
| Taki | 65% |
| Tessie | 45% |
| Tifa | 70% |
| Tyson | 100% |
| Valt Aoi | 80% |
| Vexana | 85% |
| Wakiya Murasaki | 80% |
| Ymir | 55% |
| Yotsuha | 45% |
| Yuki | 35% |
| Zero Two | 70% |

## Casual slash battle rules

`/battle player:<opponent> bey_level:50 avatars:False` challenges another player with both Beys at level 50 and all avatar bonuses/skills disabled. Level accepts 1–100; omit it to use actual owned levels. Avatars default to On. The challenge shows both rules before acceptance. Fixed level changes are match-local and include ATK/DEF/STM, HP, Special and formula/ability level gates; saved XP remains unchanged and mastery multipliers are disabled for fixed-level matches. Fixed levels require normal Beys rather than rolled boss copies. Ranked keeps its existing rules. `/commands` lists the slash battle command.
