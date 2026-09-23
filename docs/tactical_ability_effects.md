# Tactical ability effects (25 opt-in operations)

Add one operation to a Bey's `abilities[].rules[]` under `"when": "setup"`:

```json
{"name":"Pattern Reader","rules":[{"when":"setup","do":[{"op":"pattern_reader"}]}]}
```

The operation registers battle-local behavior; it does not change existing
Bey definitions. Keep the normal rule gates on the setup rule. Only the owner
gets the effect. A new Bey still receives exactly two ability effects under the
roster authoring rule; these ops are choices, not an automatic grant of 25.

| Operation | Trigger and outcome |
|---|---|
| `pattern_reader` | Repeated enemy action: 12% less incoming damage from that action this round. |
| `pressure_gauge` | No direct HP damage this round: +1 stack (max 3); next winning damaging action consumes stacks for +8% each. |
| `guard_fracture` | Two Defense wins: opponent's next Defense has 15% less effective DEF. |
| `second_wind` | First surviving drop under 30% HP: restore 20% max Stamina, once per battle. |
| `charge_overflow` | Gauge gain above 150: half the excess becomes shield, at most once per round. |
| `opening_gambit` | First Attack: +20% damage on win, or +15 Charge on loss, once. |
| `recoil_engine` | Take at least 20% max HP in one direct hit: next damaging action +15%. |
| `lasting_guard` | Defense win: 25% less incoming post-shield damage the next round. |
| `staggered_rhythm` | A/B/A action sequence: +20 Charge. A repeated action resets the sequence. |
| `finishers_mark` | Each direct damaging action adds one mark (max 3); next Special with three marks deals +25% damage and consumes marks before adding its own mark. |
| `emergency_reserve` | First unaffordable paid action: cover the Stamina shortfall at two HP per Stamina, provided HP remains positive. Automatic while equipped. |
| `shield_momentum` | Shield absorption grants one Charge per five shield damage, max 15 per round. |
| `clean_break` | The engine's `cleanse` operation removes a status: 10% less incoming damage for the current and following round. |
| `counterweight` | At round start, add DEF equal to 10% of the positive enemy ATK difference (max 20). |
| `perfect_timing` | Special used on its first available turn: refund 20% of its gauge cost after payment. |
| `rising_stakes` | Lost clash adds a stack (max 4); next winning damaging action consumes for +5% each. |
| `battle_tempo` | No Charge for three turns: next Charge gets +20 gauge. |
| `sacrificial_guard` | Team-mode adapter `redirect_ally_damage(protector, ally, damage, protection_active=True)`: shift up to 25 incoming damage to the protector. One-on-one battles have no ally and do not activate this effect. |
| `stability_anchor` | Defense type only: first Stability loss that would cause ring-out leaves 1 Stability. |
| `precision_window` | Attack type only: Attack win gives the next Attack +10 percentage points to the ability crit roll. |
| `spin_siphon` | Stamina type only: Stamina win transfers up to 2 Stamina from foe, once per round. |
| `exposed_core` | Damage through a shield primes the next Attack or Special to use 10% less enemy DEF; a fully absorbed hit does not prime it. |
| `comeback_circuit` | Behind in HP percentage and Stamina at round start: +12% to normal gauge gains (rounded to whole points). |
| `measured_strike` | Direct hit exceeding 25% of target max HP primes next damaging action for -15% damage and +8 Stamina. |
| `final_rotation` | At start of turn 8 or later: restore 15 Stamina if missing at least 15; otherwise gain 25 Charge. Once per battle. |

All 25 are opt-in; registration checks target and numeric config. Type-specific
ops reject the wrong Bey type. There is no persistent player data or roster
mutation. BattleSession owns the action-history and end-of-round hooks.
Damage changes pass through the normal shield, HP, revival and finish paths.

PvP and Story use the shared resolver. The separately implemented boss
resolver does not execute these operations. Sacrificial Guard is an adapter
for a future ally-capable resolver and has no one-on-one activation.

Focused checks: `python tools/sim_tactical_effects.py`.
