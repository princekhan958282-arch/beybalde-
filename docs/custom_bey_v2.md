# Custom Bey V2

Run `;setcustom` in the desired review channel, or `;setcustom #channel` to choose another channel. Both `;setcustom` and `; setcustom` work. Only the bot owner can configure or review submissions. Channel configuration persists per server. Newly submitted Beys automatically post a review card there; approval controls survive a bot restart. Rerun the command to recover saved pending submissions if posting failed.

The builder, ability library, image chooser, player card and review cards use native Discord Components V2. Each player has **100 ability points**, up to **two different effects**, and a separate **395-stat total** (minimum 20 per stat). Every ability costs **50 points**, so two abilities use all **100 points**. Each effect's cost is displayed beside its name. Each ability has a three-round cooldown; revival arms once per battle. Image URLs/uploads must contain a background-removed transparent PNG/WebP, with both transparent and visible pixels, at most 8 MB and 16 megapixels. Transparency is checked; the owner still checks that the image depicts an appropriate Bey.

Existing owned Custom Bey identity, stats, progression and approval data are preserved. Current server-owned preset magnitudes are applied to both new submissions and saved Customs at battle read/compilation time. Special damage remains 80–140 with none/heal/shield secondary effects. Legacy submissions without a server ID can be recovered through the owner command.

## Ability costs

16 existing effects plus 40 additions, all implemented through the shared battle engine.

| Effect | Points | Behavior |
|---|---:|---|
| Bonus Damage | 50 | Adds 10 damage when triggered. Cooldown: 4 turns. |
| Damage Boost | 50 | Adds 10% damage when triggered. Cooldown: 4 turns. |
| True Damage | 50 | Deals 8 true damage when triggered. Cooldown: 4 turns. |
| Heal | 50 | Restores 12 HP when triggered. Cooldown: 4 turns. |
| Percent Heal | 50 | Restores 4% max HP when triggered. Cooldown: 4 turns. |
| Shield | 50 | Gains a 12 HP shield; total shields cap at 10% max HP. Cooldown: 4 turns. |
| Stamina Recovery | 50 | Restores 2 Stamina when triggered. Cooldown: 4 turns. |
| Stability Recovery | 50 | Restores 5 Stability when triggered. Cooldown: 4 turns. |
| Stability Break | 50 | Removes 5 enemy Stability when triggered. Cooldown: 4 turns. |
| Stamina Drain | 50 | Drains 1 enemy Stamina when triggered. Cooldown: 4 turns. |
| Guard Window | 50 | Take 10% less damage for 2 turns. Cooldown: 4 turns. |
| Reflect Window | 50 | Reflect 10% damage for 2 turns. Cooldown: 4 turns. |
| Defense Weaken | 50 | Reduce enemy Defense by 10% for 1 turn; does not bypass defense or counters. Cooldown: 4 turns. |
| Critical Focus | 50 | Gain 5% critical chance. Cooldown: 4 turns. |
| Critical Power | 50 | Critical multiplier becomes 1.6x. Cooldown: 4 turns. |
| Cleanse | 50 | Cleanse negative effects when triggered. Cooldown: 4 turns. |
| Attack Boost | 50 | Gain 8 Attack for 2 turns. Cooldown: 4 turns. |
| Attack Surge | 50 | Gain 10% Attack for 2 turns. Cooldown: 4 turns. |
| Attack Pressure | 50 | Enemy loses 8 Attack for 2 turns. Cooldown: 4 turns. |
| Attack Shatter | 50 | Enemy loses 10% Attack for 2 turns. Cooldown: 4 turns. |
| Defense Boost | 50 | Gain 8 Defense for 2 turns. Cooldown: 4 turns. |
| Defense Surge | 50 | Gain 10% Defense for 2 turns. Cooldown: 4 turns. |
| Defense Pressure | 50 | Enemy loses 8 Defense for 2 turns. Cooldown: 4 turns. |
| Defense Shatter | 50 | Enemy loses 10% Defense for 2 turns. Cooldown: 4 turns. |
| Stamina Boost | 50 | Gain 8 Stamina for 2 turns. Cooldown: 4 turns. |
| Stamina Surge | 50 | Gain 10% Stamina for 2 turns. Cooldown: 4 turns. |
| Stamina Pressure | 50 | Enemy loses 8 Stamina for 2 turns. Cooldown: 4 turns. |
| Stamina Shatter | 50 | Enemy loses 10% Stamina for 2 turns. Cooldown: 4 turns. |
| Balanced Boost | 50 | Gain 5 ATK, DEF and STM for 2 turns. Cooldown: 4 turns. |
| Balanced Surge | 50 | Gain 6% ATK, DEF and STM for 2 turns. Cooldown: 4 turns. |
| Attack Growth | 50 | Gain 3 Attack per activation, at most 3 stacks. Cooldown: 4 turns. |
| Defense Growth | 50 | Gain 3 Defense per activation, at most 3 stacks. Cooldown: 4 turns. |
| Stamina Growth | 50 | Gain 3 Stamina per activation, at most 3 stacks. Cooldown: 4 turns. |
| HP Barrier | 50 | Gain a shield worth 5% current HP; total shields cap at 10% max HP. Cooldown: 4 turns. |
| Maximum HP Barrier | 50 | Gain a shield worth 4% max HP; total shields cap at 10% max HP. Cooldown: 4 turns. |
| Life Steal | 50 | Gain 5% lifesteal for the battle. Cooldown: 4 turns. |
| Second Wind | 50 | Arm a 10% max HP revival, once per battle. |
| HP Regeneration | 50 | Regenerate 2 HP per turn for the battle. Cooldown: 4 turns. |
| Special Charge | 50 | Add 8 damage to the next Special; stored Special bonuses cap at 25. Cooldown: 4 turns. |
| Hit Charge | 50 | Add 6 damage to the next Attack or Special; stored hit bonuses cap at 12. Cooldown: 4 turns. |
| Special Follow-Up | 50 | Add 25 damage to the next Special; stored Special bonuses cap at 25. No full-strength extra hit. Cooldown: 4 turns. |
| Finishing Blow | 50 | Deal 12 true damage if enemy HP is below 25%. Cooldown: 4 turns. |
| Enemy HP Strike | 50 | Add damage equal to 2% of enemy current HP. Cooldown: 4 turns. |
| Missing HP Drive | 50 | Add up to 12% damage as your HP falls. Cooldown: 4 turns. |
| Enemy Wound Drive | 50 | Add up to 10% damage as enemy HP falls. Cooldown: 4 turns. |
| Stamina Drive | 50 | Add up to 8% damage based on your stamina ratio. Cooldown: 4 turns. |
| Attack True Strike | 50 | Deal true damage equal to 3% of your Attack. Cooldown: 4 turns. |
| Defense True Strike | 50 | Deal true damage equal to 3% of your Defense. Cooldown: 4 turns. |
| Stamina True Strike | 50 | Deal true damage equal to 3% of your Stamina. Cooldown: 4 turns. |
| Heavy Guard | 50 | Take 15% less damage for 2 turns. Cooldown: 4 turns. |
| Long Guard | 50 | Take 8% less damage for 2 turns. Cooldown: 4 turns. |
| Heavy Reflect | 50 | Reflect 15% damage for 2 turns. Cooldown: 4 turns. |
| Long Reflect | 50 | Reflect 8% damage for 2 turns. Cooldown: 4 turns. |
| Burst Recovery | 50 | Restore 8 Stability. Cooldown: 4 turns. |
| Burst Pressure | 50 | Remove 7 enemy Stability. Cooldown: 4 turns. |
| Deep Drain | 50 | Drain 2 enemy Stamina. Cooldown: 4 turns. |

## Validation

`python -m unittest tools.test_custom_bey_v2 tools.test_four_burst_beys tools.test_special_avatar_pipeline -q`

Live Discord sending and deployed bot behavior require a post-deployment smoke test: configure channel, submit transparent image, verify routing, restart, approve or reject, then equip and battle.

## V3 battle safeguards

Repeatable preset abilities use a 4-turn cooldown. Defense Weaken applies a 10% DEF debuff for one turn; it never arms full defense bypass. Special Follow-Up adds 25 flat damage to the next Special instead of a duplicate full-strength hit. Stored Special bonuses cap at 25, stored Attack/Special bonuses at 12, and shields from Custom presets/riders cannot increase the total shield above 10% of max HP. Second Wind remains once per battle. Same-named player designs compile independently. Official Bey definitions retain their authored operations and values.
