# Battle structure follow-up

## Shared rules with existing adapters

`combat_rules.py` owns the normal damage formula, final HP rounding, actual HP damage/healing, resource spending/recovery/caps, Stamina Stat / 20 initialization, and the Stamina-type cost discount. The existing session and boss resolvers call those helpers. `type_gimmicks.py` owns passive stat factors, Defense reduction, activation, suppression, critical eligibility, Kinetic nullification/mitigation/exact returns, Overdrive and Morph.

The Discord session and boss/search adapters still orchestrate their own turns. Their authored action matchup modifiers, boss phases, normal regeneration policies, gauges, party rotation and finish integrations remain explicit adapter policies. This avoids replacing the boss system or silently changing unrelated tuning. It does not claim every boss rule equals a normal PvP rule.

Boss BladeKit offensive bonuses and damage reduction now enter the hit before HP commit. The order is authored move effects → offensive bonuses → Critical Strike → defensive kit/crystal effects → Defense-type mitigation/Kinetic Counter → HP damage → terminal returns/reflection → recovery. Old report refunds are accepted only for detached callers without `mitigation_applied`. They cannot run again on live reports. Kinetic returns suppress an additional kit reflect from the same hit and preserve fractional actual HP remainders without a second rounding step.

## Type compatibility cleanup

Removed the old Special type multiplier/gate/partial-pierce blocks from AttackManager. Conditional Special pierce is consumed by AbilityEngine at the shared mitigation stage, with its log preserved. The `combat_v3` version flag is removed; a real session always owns a centralized type engine. Standalone legacy test adapters without that engine retain their existing fallback preprocessing.

`TypeModifiers` remains import-compatible for stability/display and old external helper callers, but its passive values delegate to the new type owner. The existing stability advantage chart and attrition remain unchanged. Legacy signature helpers are not part of the live damage pipeline.

## One duration boundary

After existing start-of-turn DoT/grind checks, the session expires buffs, silence and universal durations for **both** sides before evaluating combat stats or resolving either hit. DamageFilter skips its fallback tick when that boundary has already run. Effects granted during action resolution keep their full duration until the next boundary; multi-hit Specials do not age them per hit.

This deliberately fixes the stale-stat case where a duration-one stat buff was included in the damage formula even though its status had expired before defensive processing. Other existing end-of-round lifetimes, including invulnerability and ability-owned windows, retain their own cleanup.

## Authored gimmick controls

Four shared AbilityEngine operations:

| Operation | Fields | Meaning |
| --- | --- | --- |
| `gimmick_chance` | `gimmick`, `value`, `turns` | Add percentage points to the natural roll, clamped to 0–100%. Negative values reduce chance. |
| `gimmick_force` | `gimmick`, `turns`, optional `allow_actions` | Force a gimmick, including on another Bey type. Critical Strike still requires Attack unless an action such as Special is explicitly allowed. |
| `gimmick_suppress` | `gimmick`, `turns`, optional `target: enemy` | Suppress natural/forced activation; suppression wins. |
| `gimmick_bonus` | `gimmick`, `bonus`, `value`, `turns` | Modify Critical damage, Morph stats, or Overdrive recovery. |

Supported bonuses: Critical `damage`, Morph `stats`, Overdrive `heal` use percentages. Overdrive `stability` and `stamina` use flat recovery points. For example, Critical `damage: -100` removes one multiplier point from 2x. Unknown gimmicks/bonus fields raise an explicit configuration error.

Controls from the same caster/source refresh instead of stacking. Distinct ability sources may combine. Transient controls expire, Morph remains a three-round refreshed effect, and no values are saved to Bey data.

Use `when: on_round_start` for controls that should affect this turn's chance roll or effective stats. Other existing triggers can force/suppress a pending hit through the normal AbilityEngine; chance/stat changes granted after stats/rolls are evaluated affect future evaluations. Existing conditions, per-op gates and once-per-battle rules still apply.

```json
{
  "name": "Critical Lock",
  "rules": [{
    "when": "on_round_start",
    "if": [{"cond": "my_move_is", "value": "attack"}],
    "once": "battle",
    "do": [{"op": "gimmick_force", "gimmick": "critical_strike", "turns": 1}]
  }]
}
```

The boss gimmick adapter reuses actual AbilityEngine conditions/gates/operations rather than introducing another DSL. It projects HP, actions, levels, stamina, gauge and stability, persists once-state and timed controls, and detaches search clones. It supports round/setup/passive/threshold, move-result, hit/Special and defensive control triggers. Generic counter/mode/debuff-history conditions are not projected by the existing BladeKit system; such controls are skipped with an explicit diagnostic instead of assuming missing data satisfies a condition. This bridge does not pretend to port the entire AbilityEngine to boss combat.

Story AI snapshots carry live Morph and timed controls and avoid double-applying Morph/passives. Search remains deterministic and does not roll natural random gimmicks or probabilistic authored procs. No battle button contains new gimmick rules.

## Validation

53 focused tests pass through real session turns, boss resolution, a real BossFight step and named Specials. New coverage includes both player orders, expiry, newly granted buffs, explicit cross-type/Special forcing, chance/suppression, refresh/expiry, once/conditions, Morph/Overdrive bonuses, search isolation, Story projection, shield ordering, kit mitigation and offense inside exact returns, and fractional capped returns.

Syntax and whitespace checks pass. All 1,452 checks/tests passed across 20 existing battle, type, button, tactical, resource, selected ability, ranked, Story, Argus, boss-tier/flow and avatar suites. Azure's standalone fixture now owns the real type engine and expects Defense's 14% reduction; its conditional-pierce check remains intact.

The IQ benchmark was rerun: 46 checks pass and three existing assertions fail. IQ 1 wins about 94.4%, IQ 5 100%; the gap is 5.6 percentage points. Live-kit coverage remains 104/127 (~81.9%), below 85%. This structure PR does not solve the difficulty or general boss AbilityEngine coverage problems, and does not claim production balance is ready.

No production Discord deployment was performed. Tests use fake transport/in-memory profiles. No Bey definitions, stored player stats, commands, UI wiring or reward systems are changed.
