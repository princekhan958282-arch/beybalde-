# Ten opt-in ability effects

These effects extend `abilities[].rules[].do[]` in the shared AbilityEngine.
They run in PvP and Story battles. No existing Bey definition is changed
and no database migration is needed. Boss battles use a separate resolver:
`BladeKit.unsupported()` reports these operations, even in an otherwise supported
ability. They do **not** run in that resolver.

Rules retain their existing `when`, `if`, `chance`, `once`, and per-op `_if` gates.
Use existing `not_on_cooldown` conditions and `start_cooldown` operations for
activation cooldowns. Use `on_special` for once-per-Special activation;
`on_hit` and other per-hit triggers can intentionally apply multiple times.

## Authoring reference

Percentages use percentage points: `50` means 50%, not `0.5`. Named effects
refresh rather than stack when caster, recipient, operation and name match.
Use distinct names for independently stacking effects. Durations count round
ends, including the activation round. Defaults for timed windows: 2 rounds.
Windows, marks, damage banks and echo queues belong to one battle only.

| Effect | Example operation | Behavior |
|---|---|---|
| Damage storage | `{"op":"damage_storage","name":"impact","pct":25,"cap":100,"turns":4}` | Stores a percentage of actual direct HP damage received. Explicit cap required. |
| Release stored damage | `{"op":"release_storage","name":"impact"}` | Companion operation for storage. Consumes the named bank before a protected, nonrecursive secondary hit. A blocked release still spends the bank. |
| Debuff conversion | `{"op":"debuff_conversion","prevent_pct":50,"gain_pct":5,"max_pct":15,"turns":4}` | Reduces hostile stat debuffs/burn magnitude or silence duration. Only actually prevented amounts grant power. Power boosts the next damaging action, including its multi-hit total, and is spent only if HP damage lands. |
| Buff suppression | `{"op":"buff_suppression","target":"enemy","pct":50,"effects":["attack","defense"],"turns":2}` | Reduces positive stat bonuses from StatusManager and Domain stats. Negative bonuses and base stats stay intact. Original buffs retain their own expiry; suppression does not remove or restore them. Strongest applicable suppression wins. Does not suppress shields, damage amps or unrelated passive fields. |
| Resource conversion | `{"op":"resource_conversion","from":"gauge","to":"stability","amount":10,"rate":0.5}` | Spends the full requested amount if available and restores up to the destination cap. No payment if destination is full. Supports gauge, stamina and stability; only own resources. Cannot exhaust stamina/stability to zero. Gauge/stability payments must be whole points. |
| Delayed echo | `{"op":"delayed_echo","pct":15,"cap":80,"moves":["special"],"delay":1,"turns":4}` | Records actual direct HP damage once per entire action. A delay of 1 fires at the end of the following round. Once queued, an echo survives expiry of its registration but cannot fire with either participant defeated. Explicit cap required. |
| Threshold marks | `{"op":"threshold_mark","name":"purity","target":"enemy","threshold":2,"turns":3,"do":[{"op":"enemy_debuff","stat":"defense","value":20,"turns":2}]}` | Each trigger adds `count` (default 1), capped at threshold. Reaching threshold consumes the marks and executes its payload once. Payload uses the original caster/foe perspective. Marks refresh their expiry on application. |
| Action cost modifier | `{"op":"action_cost_modifier","target":"enemy","resource":"stability","moves":["attack","special"],"pct":30,"turns":3}` | Changes existing costs of selected moves. Stamina quotes, affordability and deduction share one path. Stability only changes self action costs, never enemy-inflicted stability loss. Zero-cost actions remain free. |
| Cooldown shift | `{"op":"cooldown_shift","name":"phoenix_nova_cd","rounds":-1}` | Adjusts an already-running named engine cooldown. Negative reduces; positive extends. Clamped to 0–99 rounds. Does not invent cooldowns, refill gauge or bypass resource requirements. `target: enemy` is supported. |
| Shield shatter | `{"op":"shield_shatter","pct":100,"turns":2}` | +100% shield damage means double damage to shields. Overflow returns to normal damage units: 40 damage against a 50 shield leaves 15 HP damage. No extra HP damage if no shield exists. |
| Effect transfer | `{"op":"effect_transfer","count":1,"effects":["attack","defense","stamina","burn","silence"]}` | Transfers eligible hostile timed stat penalties, then silence, then burn, preserving remaining duration. Permanent penalties and self tradeoffs are excluded. Receiver immunity/ward/avatar resistance can block it. A fully prevented application leaves the source effect in place. Existing receiver burn is not overwritten. |

Storage and release are two operations implementing one of the ten effects.
Defaults: target `self`; name is the operation name; mark threshold 3 and expiry
3 rounds; transfer count 1; echo delay 1 and move filter `special`. Explicitly
name storage and its release with the same name. Storage, conversion, echo,
shatter and transfer are self operations. Transfer moves your effects to the foe.

## Example ability

```json
{
  "name": "Stored Impact",
  "description": "For four rounds, store 25% of direct damage taken (max 100). Release it on a Defense win.",
  "rules": [
    {"when":"setup","do":[{"op":"damage_storage","name":"impact","pct":25,"cap":100,"turns":4}]},
    {"when":"on_defense_win","do":[{"op":"release_storage","name":"impact"}]}
  ]
}
```

## Protection and bounded state

- Invalid new numeric values, durations, targets and filters are rejected with
  a battle log. Values must be finite. Durations/counts are integers from 1–99.
- Percentage fields are 0–100 except action cost modifiers (-90 to +300).
  Combined cost modifiers are also clamped to that range. Nonzero costs remain
  positive; stability rounds up to a whole point.
- Self stat tradeoffs (`hostile=False`) do not feed debuff conversion.
  Existing Purification Domain reduction applies first; conversion reduces
  only the remainder and grants its own bounded next-action power.
- Conversion banks use the greatest applicable prevention window per incoming
  effect. Their combined outgoing bonus cannot exceed 100%.
- Storage and echoes record committed direct action damage, after avatar
  protection and HP clamping. Reflections, damage over time, echoes and storage
  releases do not fill those banks or schedule further echoes.
- Secondary damage checks invulnerability, ability evasion, shields, timed
  resistance, avatar absorption/immortality and Bey revival. It does not rerun
  offensive/defensive ability triggers or lifesteal, and does not reapply the
  source action's ATK/type scaling.
- Up to 128 windows, 128 mark groups and 64 queued echoes per battle.
  Threshold payloads contain at most eight operations from `enemy_debuff`,
  `enemy_debuff_pct`, `buff`, `shield`, `log`; recursive payloads are rejected.
- Battle logs report application, consumption and expiry. Status snapshots
  expose independent copies under `extended_effects`. No live battle state is
  persisted across a restart; saved JSON definitions are loaded normally.

## Verification

Run `python tools/sim_extended_effects.py` for focused manager integration tests.
The suite includes JSON definition roundtrips, resource payments, real Special
cooldown state, multi-hit conversion, shielding, avatar dodges, revival, expiry,
nonrecursive damage, transfer failures and boss compatibility reporting.

Also verified: Kirindael and Domain, Cosmic Phoenix, battle ticks, button costs,
status snapshots, type rules, Story, boss tiers and balance-pass simulations.
The older avatar-skills and Radiant Valkyrie suites have failures reproduced
identically on unchanged baseline `d566cac`; they are not claimed to pass.
Live Discord interactions and the separate boss resolver's execution of these
new effects are not covered or supported by this change.
