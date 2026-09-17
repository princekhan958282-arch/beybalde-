# Lightning Purifier rework — implementation and wiring audit

## Implemented in shared PvP/Story combat

- One hit: `ceil(145 + 0.40 ATK + 0.50 DEF + 0.80 STM)`, using live leveled stats, parts, mastery and avatar bonuses. The old Special-stat multiplier is not applied again. Normal damage defenses still apply.
- Cast opens the Domain and cleanses burn, silence, negative stat effects and Grind even if the hit is blocked. The existing 60 Purifier Charge plus gauge gate is retained; storage cap and Horn/Spark charge generation are unchanged.
- Four rounds, counting the activation round. Effects expire after the fourth round's action costs. Recasting replaces the previous Domain; it does not stack temporary stability.
- Owner: +20% base ATK/DEF, +15% temporary maximum/current Stability, -25% action Stability costs. Temporary stability is removed on expiry, retaining costs already spent; zero remaining stability is checked for ring-out immediately.
- Enemy: -5% base ATK/DEF, +30% action Stability costs, -25% healing/lifesteal. Direct enemy-inflicted stability damage and counters are not action costs.
- Domain overrides Horn's full immunity. Hostile numeric stat debuffs and burn retain half strength; binary silence and Grind retain half duration, rounded up. Self-authored stat tradeoffs do not grant conversion. Conversion grants +5%, capped at +15%, on the next damaging action, including all its hits. Fully blocked hits retain the bank.
- Each used Special and each triggered non-passive avatar skill grants a mark. Multiple hits/rules of the same Special/skill in a round count once. At two marks, Judgment deals `ceil(40 + 0.15 live ATK)` true damage once per Domain. It bypasses shields/defense, but honors existing revival rules.
- Ability heals, lifesteal, regeneration, drain heals, chain heals, Stamina-action healing and revival HP pass through the healing penalty once.
- Profile/info/beypedia formula totals and battle Domain status are displayed. Story AI uses the stat formula for damage scoring and retains the same Special eligibility gate. AI lookahead remains an approximation, as it is for other abilities.

## Wiring inspected

`BattleSession` button validation / `special_gate` → `AttackManager` Special resolver → `AbilityEngine` cast/skill hooks → `StatusManager` / `purification` state → live stats, healing and Stability managers → end-of-round expiry / ring-out → battle display.

Story runs the same `BattleSession`; its AI uses `special_gate.ready`. Horror's explicit Special-nullification hook still exits before casting; `non_damage` also takes precedence over formula damage.

The Domain is transient battle state. No player database migration is required. The roster data is committed; a fresh process reloads it. Existing ownership, rarity and acquisition rules are unchanged.

## Outstanding raid limitation — not full cross-mode parity

`cogs/battle/boss/boss_battle.py` uses `boss_ai.Fighter` plus a simplified static `BladeKit`, rather than `BattleSession`, `AbilityEngine` or `StatusManager`. It has no shared Stability bar, debuff lifecycle, avatar-skill dispatch or Purifier Charge gate. `BladeKit` does not implement `purification_domain`. Consequently raid bosses do **not** receive the complete new rework in this PR. The `Fighter.special_damage` field added here is used for Story AI scoring, not an assertion of raid support.

A raid adapter or migration to the shared battle engine remains necessary. This PR should remain a draft until that gap is resolved or the release scope is explicitly limited to PvP/Story. No live Discord match or deployment was performed.

## Validation

- `python tools/sim_kirindael.py`: 88 checks pass.
- `python tools/sim_kirindael_domain.py`: 15 tests pass, including real Special resolution at Lv100, Azure Drakonyx and Cosmic Phoenix, revival, immunity, silence, multi-hit deduplication, healing, costs, expiry, recast and mirror domains.
- `python tools/sim_cosmic_phoenix.py`: 57 checks pass.
- `python tools/sim_button_effects.py`: 92 checks pass.
- `python tools/sim_story.py`: 197 checks pass.
- `sim_avatar_skills.py` stops on `no weight for bonus key 'stability_percent'`. Reproduced on unmodified base commit `e58230d`; not caused by this change.
