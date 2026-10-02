# Battle combat rework — implementation and validation

Status: review draft. The requested arithmetic and real round tests pass. The boss IQ balance benchmark fails its old difficulty targets; this change is not approved for production deployment.

## Inspection before implementation

| Concern | Existing owner and integration |
|---|---|
| Commands | `cogs/battle/battle.py`; retained |
| Buttons | `cogs/battle/ui.py` and `session.py`; action selection reaches `submit_move()` |
| State and turn resolution | `BattleSession` in `session.py`; Story, ranked and tournament use this session |
| Damage | `damage_rules.calc_damage()` called by `AttackManager.resolve_pair()` |
| Specials | `damage_rules.resolve_special[_hits]()`, `AttackManager._resolve_special()`, ability Special hooks |
| Resources | `StaminaManager` for Battle Stamina and Special gauge; `StabilityManager` for stability |
| Types | `abilities/type_system.py`; old passives were stat-scaled and gated by matchup |
| Stat modifiers | parts, avatar and progression at session construction; live status buffs through `purification.effective_stats()` |
| Abilities | `AbilityEngine.apply()`, `DamageFilter`, extended/tactical effects, chains, per-hit Special hooks |
| Finishes | session ring-out/stamina/HP checks, revival hooks and ranked finish recording |
| Bosses | existing `boss_ai.Fighter`/`resolve()` and named Special branch in `BossFight._fire_special()` |
| AI | Story projects its real session into Fighters for planning; affordability uses the live stamina manager |

Conflicts addressed: legacy shield gate/deficit bleed; natural crits for all types; matchup-gated type passives; double application of effective Attack/Defense buffs; interrupted/low-HP boosted recovery; duplicated named boss Special arithmetic; tactical modifiers being lost when effective stats are refreshed.

## Exact formulas and decisions

Normal base damage is `(((2 * level / 5 + 2) * move_power * effective_attack / max(1, effective_defense)) / 50) + 2`. Keep the float through the core calculation. Final HP damage is `max(0, floor(damage))`. Existing authored ability operations retain their own integer rounding, shield accounting and damage definitions.

Move Power defaults to 100, with a safe `button_profile.<action>.move_power` override. Level uses the Bey level, including Story's NPC level and the player's actual owned Bey level in boss fights.

The existing action wheel is retained: normal Attack versus Attack uses 1.0; versus Defense uses 0.5; versus Stamina uses 1.2; versus Charge uses 1.5; versus Special uses 0.5. Defense versus Attack returns no duplicate outgoing hit because the Attack pairing already supplies the ordinary guard counter. Other Defense damage also uses the core formula. The ordinary guard counter remains `round(10 + min(50, max(0, Defense) * 0.25))`. Existing Grind, stability costs, gauges, attrition and finish priority are retained.

Attack passive multiplies effective Attack by 1.10. Defense passive multiplies mitigatable incoming damage by 0.86. Stamina multiplies the composed move cost by 0.80. Balance multiplies HP capacity, Attack, Defense and Stamina by 1.033. These are battle-local and apply regardless of matchup. The existing type triangle remains available to the existing stability rules.

Starting Battle Stamina is `effective_stamina_stat / 20`, capped by the existing maximum. The existing maximum remains `min(30, 11 + effective_stamina_stat / 20)`. It is not permanently saved. Normal Battle Stamina recovery and passive regeneration remain existing mechanics, distinct from HP healing.

Normal Stamina action: `floor(effective_stamina_stat * 0.50)` HP recovery, or 0.70 for Stamina type, and +8 Stability. The prior interruption and low-HP healing multipliers do not apply to these standard actions. Existing explicit healing debuffs such as Purification still apply. Cap against each player's current maximum.

All natural gimmicks have a 5% chance. Roll before either side resolves: Attack/Critical Strike only on Attack; Defense/Kinetic Counter only on Defense; Stamina/Overdrive only on Stamina; Balance/Adaptive Morph on a battle action.

Critical Strike doubles offensive Attack damage before defensive filters. It does not naturally activate for Specials or indirect damage. Explicit ability and avatar crits remain available.

Kinetic Counter nullifies a non-Critical normal Attack without consuming its defender's shield. On a mitigatable Special it applies an additional 0.70 multiplier. True damage bypasses the new type reductions. Authored defense bypass and partial Special pierce remain supported.

Critical Strike plus Kinetic Counter applies the hit normally, including shields/avatar mitigation and the defender's current HP limit. Return exactly the committed HP loss as tagged, terminal counter damage. Do not add ordinary guard, ability-reflect or avatar-counter damage to that exact return. The return cannot re-enter the damage/ability pipeline.

Overdrive replaces normal HP/resource/Stability recovery with 100% of effective Stamina Stat, +30 Stability and +6 Battle Stamina. It is not 70% plus 100%, nor +8 plus +30. Respect existing caps.

Adaptive Morph lasts three rounds including the activation round. Multiply effective core stats by another 1.05 while active. Refresh its duration on another activation; never multiply another 1.05 into stored stats. HP capacity rises temporarily without granting a free heal; clamp current HP when capacity returns. Preserve separate ability-driven max-HP changes.

The central engine has battle-local chance bonuses, forced/suppressed gimmicks and Morph/Overdrive/critical extension points. Existing form evolution updates its type identity without rolling another gimmick during the same action.

## Compatibility changes

- Main sessions deep-copy incoming Bey dictionaries so an existing form evolution cannot mutate stored definitions.
- Effective Attack/Defense stat buffs are consumed by the formula once, instead of being added again as flat damage.
- Defensive filters follow offensive effects and the type critical; Special avatar flat/Attack riders also go through mitigation.
- Tactical round-start modifiers run after effective-stat refresh so Counterweight/Exposed Core survive.
- Stamina recovery resolves after HP damage and counter damage; a defeated Bey cannot recover itself without an explicit revival ability.
- Move submission validates affordability and Special readiness before committing the action.
- Main sessions retain their existing status/ability lifecycle; standalone older test harnesses remain supported.
- Boss normal damage uses the same core helper. Existing boss Special multipliers, boss state effects and player-Special-versus-boss modifier remain. Named boss Specials delegate exchange bookkeeping to the existing boss resolver, then apply their named effects.
- Boss/player standard healing follows the new percentage formula; the old boss-resolver healing interruption, decay and lifetime-budget rules conflict with that formula and no longer constrain standard Stamina actions. This contributes to the balance result below.
- AI search clones preserve type, level, Morph, caps and costs. Search is deterministic and excludes random natural procs; live combat rolls them. Story still uses its existing lossy projection for planning, not a replacement live resolver.
- The pre-damage ring-out check now records `ringout` before ending the battle, fixing an existing Burst mislabel.

## Validation

`python -m compileall -q cogs tools tests` and `git diff --check` passed.

`python -m unittest discover -s tests -v`: **33 passed**. Includes formula ratios and zero Defense; all gimmick action gates and 5% boundary; passives and composed costs; buffs/debuffs; both player orders; capped critical returns with reflect/avatar effects; Specials and true damage/shields; recovery caps; non-stacking Morph; button validation; form evolution; Burst/Ring-Out/Spin/Draw; and boss resolver/search-clone paths.

Passing existing suites: battle ticks, tactical PvP, tactical effects, extended effects, ranked (211 checks), buttons, types, Kirindael Domain, stat pipeline, Cosmic Phoenix, multi-hit nerf, Guilty/Hades, Heaven's Ring, Azure Drakonyx, Odax/Horusood, boss tiers, boss flow, avatars, Story (197 checks), and Argus (119 checks).

Updated synthetic fixtures to use explicit level 100 when testing sizeable damage thresholds. Updated expectations that explicitly depended on the replaced type passives. Ability thresholds and Bey data were not retuned. Corrected the old ranked source assertion that assumed only two ring-out sites although main already had three.

Verified on unchanged main as well as this branch: failures in `sim_levels` (two authored Specials do not grow), `sim_radiant_valkyrie` (stale expected DSL op), `sim_tartarus` (mock signature), `sim_kirindael` and `sim_aegis_valorian` (incomplete mock engines), `sim_ultimate_valkyrie` (empty iterable), and `sim_shadow_dragon` (incomplete status mock). `sim_avatar_skills` also fails on stale card/catalogue assertions and an unweighted `stability_percent` key; these failures were reproduced on unchanged main.

The initial full IQ benchmark at implicit level 1 exceeded 240 seconds. Re-run at explicit level 100 completed: **46 checks passed, 3 failed**. The boss difficulty ladder is monotonic, but IQ 1 wins approximately **95.2%**, and the gap to IQ 5 is approximately **4.8 percentage points**, below the old targets. Its separate 85% live-kit catalogue assertion reports 104/127, approximately 81.9%. Preserve this evidence: do not weaken win-rate assertions or silently retune roster values to hide the result. Boss difficulty needs a deliberate balance pass before production.

No live Discord deployment or authenticated production database battle was run. End-to-end coverage uses real session/resolver code with fake Discord transport and an in-memory user store. Test-driven data migrations were removed from the working diff; this change does not alter `data/beyblades.json`.

## Changed files

Combat: `cogs/battle/type_gimmicks.py`, `damage_rules.py`, `button_profile.py`, `session.py`, `attack_manager.py`, `defense_manager.py`, `stamina_manager.py`, `damage_filter.py`, `purification.py`.

Abilities: `cogs/abilities/ability_engine.py`, `type_system.py`.

Existing integrations: `cogs/battle/boss/boss_ai.py`, `boss_battle.py`, `cogs/story/story_ai.py`.

Tests: `tests/test_combat_rework.py`; `tools/sim_types.py`, `sim_tactical_pvp.py`, `sim_story.py`, `sim_ranked.py`, `sim_azure_drakonyx.py`, `sim_argus_boss.py`, `sim_boss_iq.py`.

Documentation: this report. Commands, Bey data, ability definitions, Special definitions, reward logic and unrelated systems are retained.

## Follow-up bug pass

Four recovery regressions were covered with new tests. The boss resolver capped healing before damage, preventing full-HP fighters from recovering damage taken in the exchange. It also granted Stability/Battle Stamina and logged healing for knocked-out Stamina users. Named boss Specials discarded the player's healing report. Their drain report could exceed actual HP recovery at the cap.

Healing now resolves after direct and terminal counter damage, with alive checks before recovery. Named Specials retain shared-resolver healing and report only capped drain recovery. Focused tests cover both fighter orders, lethal damage, Overdrive caps, named Argus Specials, and capped drain. All 33 focused tests pass; syntax and whitespace checks pass. This pass does not retune boss difficulty. The prior IQ benchmark failures above remain a release blocker; that benchmark was not rerun in this follow-up.

Follow-up regression run: 1,044 checks/tests passed across battle ticks, tactical PvP, extended effects, ranked, types, stat pipeline, boss tiers, boss flow, Story, Argus, and avatars. No new failures in these suites.
