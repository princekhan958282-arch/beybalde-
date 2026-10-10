# Custom Bey V2

Run `;setcustom` in the desired review channel, or `;setcustom #channel` to choose another channel. Both `;setcustom` and `; setcustom` work. Only the bot owner can configure or review submissions. Channel configuration persists per server. Newly submitted Beys automatically post a review card there; approval controls survive a bot restart. Rerun the command to recover saved pending submissions if posting failed.

The builder, ability library, image chooser, player card and review cards use native Discord Components V2. Each player has **100 ability points**, up to **two different effects**, and a separate **395-stat total** (minimum 20 per stat). Every ability costs **50 points**, so two abilities use all **100 points**. Each effect's cost is displayed beside its name. Each ability has a three-round cooldown; revival arms once per battle. Image URLs/uploads must contain a background-removed transparent PNG/WebP, with both transparent and visible pixels, at most 8 MB and 16 megapixels. Transparency is checked; the owner still checks that the image depicts an appropriate Bey.

Existing owned Custom Bey data is preserved. New rules apply to new submissions. Special damage remains 80–140 with none/heal/shield secondary effects. Legacy submissions without a server ID can be recovered through the owner command.

## Ability costs

16 existing effects plus 40 additions, all implemented through the shared battle engine.

| Effect | Points | Behavior |
|---|---:|---|
| Bonus Damage | 50 | Adds 15 damage when triggered. |
| Damage Boost | 50 | Adds 15% damage when triggered. |
| True Damage | 50 | Deals 12 true damage when triggered. |
| Heal | 50 | Restores 18 HP when triggered. |
| Percent Heal | 50 | Restores 8% max HP when triggered. |
| Shield | 50 | Gains a 20 HP shield when triggered. |
| Stamina Recovery | 50 | Restores 3 Stamina when triggered. |
| Stability Recovery | 50 | Restores 8 Stability when triggered. |
| Stability Break | 50 | Removes 8 enemy Stability when triggered. |
| Stamina Drain | 50 | Drains 2 enemy Stamina when triggered. |
| Guard Window | 50 | Take 15% less damage for 2 turns. |
| Reflect Window | 50 | Reflect 15% damage for 2 turns. |
| Defense Pierce | 50 | Ignore enemy defense for 1 turn. |
| Critical Focus | 50 | Gain 10% critical chance. |
| Critical Power | 50 | Increase critical damage. |
| Cleanse | 50 | Cleanse negative effects when triggered. |
| Attack Boost | 50 | Gain 12 Attack for 2 turns. |
| Attack Surge | 50 | Gain 15% Attack for 2 turns. |
| Attack Pressure | 50 | Enemy loses 12 Attack for 2 turns. |
| Attack Shatter | 50 | Enemy loses 15% Attack for 2 turns. |
| Defense Boost | 50 | Gain 12 Defense for 2 turns. |
| Defense Surge | 50 | Gain 15% Defense for 2 turns. |
| Defense Pressure | 50 | Enemy loses 12 Defense for 2 turns. |
| Defense Shatter | 50 | Enemy loses 15% Defense for 2 turns. |
| Stamina Boost | 50 | Gain 12 Stamina for 2 turns. |
| Stamina Surge | 50 | Gain 15% Stamina for 2 turns. |
| Stamina Pressure | 50 | Enemy loses 12 Stamina for 2 turns. |
| Stamina Shatter | 50 | Enemy loses 15% Stamina for 2 turns. |
| Balanced Boost | 50 | Gain 8 ATK, DEF and STM for 2 turns. |
| Balanced Surge | 50 | Gain 10% ATK, DEF and STM for 2 turns. |
| Attack Growth | 50 | Gain 4 ATK per activation, at most 4 stacks. |
| Defense Growth | 50 | Gain 4 DEF per activation, at most 4 stacks. |
| Stamina Growth | 50 | Gain 4 STM per activation, at most 4 stacks. |
| HP Barrier | 50 | Gain a shield worth 10% of current HP. |
| Maximum HP Barrier | 50 | Gain a shield worth 8% of max HP. |
| Life Steal | 50 | Gain 10% lifesteal for the battle. |
| Second Wind | 50 | Arm a 15% max HP revival; activates once per battle. |
| HP Regeneration | 50 | Regenerate 4 HP per turn for the battle. |
| Special Charge | 50 | Add 12 damage to the next Special. |
| Hit Charge | 50 | Add 10 damage to the next Attack or Special. |
| Extra Special Hit | 50 | Arm one extra hit on the next Special. |
| Finishing Blow | 50 | Deal 20 true damage if enemy HP is below 25%. |
| Enemy HP Strike | 50 | Add damage equal to 4% of enemy current HP. |
| Missing HP Drive | 50 | Add up to 25% damage as your HP falls. |
| Enemy Wound Drive | 50 | Add up to 20% damage as enemy HP falls. |
| Stamina Drive | 50 | Add up to 15% damage based on your stamina ratio. |
| Attack True Strike | 50 | Deal true damage equal to 6% of your ATK. |
| Defense True Strike | 50 | Deal true damage equal to 6% of your DEF. |
| Stamina True Strike | 50 | Deal true damage equal to 6% of your STM. |
| Heavy Guard | 50 | Take 25% less damage for 2 turns. |
| Long Guard | 50 | Take 12% less damage for 4 turns. |
| Heavy Reflect | 50 | Reflect 25% damage for 2 turns. |
| Long Reflect | 50 | Reflect 12% damage for 4 turns. |
| Burst Recovery | 50 | Restore 15 Stability. |
| Burst Pressure | 50 | Remove 12 enemy Stability. |
| Deep Drain | 50 | Drain 3 enemy Stamina. |

## Validation

`python -m unittest tools.test_custom_bey_v2 tools.test_four_burst_beys tools.test_special_avatar_pipeline -q`

Live Discord sending and deployed bot behavior require a post-deployment smoke test: configure channel, submit transparent image, verify routing, restart, approve or reject, then equip and battle.
