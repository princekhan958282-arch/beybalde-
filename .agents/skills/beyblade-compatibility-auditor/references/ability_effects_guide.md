# Beyblade Ability Effects & Translation Guide

This guide provides a standardized, easy-to-follow protocol for analyzing human-written Beyblade ability descriptions and translating them into deterministic, high-performance rule sets for the `AbilityEngine`.

---

## 1. The Anatomy of an Ability Description

Human-written ability descriptions usually combine **thematic lore (flavor text)** with **concrete battle mechanics**. When reading an ability, dissect it into three distinct components:

```
[Flavor / Chassis Context] + [Trigger Condition] + [Mechanical Operations]
```

### Example Decomposition: Nebula Resonance
> *"Because of the four distinct cosmic quadrants on its chassis, every successful physical Attack has a 25% chance to trigger a secondary stardust shockwave. This deals bonus damage equal to 30% of Attack and heals 120 HP, but consumes a small amount of extra Stamina."*

1. **Flavor text (ignore for mechanics)**: *"Because of the four distinct cosmic quadrants on its chassis..."*
2. **Trigger**: *"every successful physical Attack"* $\rightarrow$ `on_attack_hit`
3. **Filter / Condition**: *"has a 25% chance"* $\rightarrow$ `"chance": 0.25`
4. **Operations**:
   - *"deals bonus damage equal to 30% of Attack"* $\rightarrow$ `op: "bonus_damage_stat"`, `stat: "attack"`, `scale: 0.3`
   - *"heals 120 HP"* $\rightarrow$ `op: "heal"`, `value: 120`
   - *"consumes a small amount of extra Stamina"* $\rightarrow$ `op: "consume_stamina"`, `value: 1.0`

---

## 2. Trigger Reference Catalog

Triggers tell `AbilityEngine` **when** to fire. All triggers are declared in `cogs/abilities/ability_engine.py:TRIGGERS`.

| Trigger | Description & Typical Lore Phrasing |
| :--- | :--- |
| `on_attack_hit` | Fired when the owner's physical Attack lands successfully (*"when attacking", "on successful hit"*). |
| `on_hit` | Fired on **every** individual hit of an Attack or multi-hit Special move (*"with each strike"*). |
| `on_special` | Fired once when owner unleashes their Special move (*"when activating Special", "on Special launch"*). |
| `on_special_last_hit` | Fired on the **final hit** of a Special move (*"the final strike applies...", "finishing hit"*). |
| `on_defend` | Fired when owner selects Defense (*"when defending", "while in defensive stance"*). |
| `on_take_damage` | Fired when owner absorbs an incoming hit (*"upon taking damage", "when struck"*). |
| `on_low_hp` | Fired when HP falls below a threshold (default 50%, or custom `if: [{"cond": "hp_below_pct", "value": X}]`). |
| `on_high_hp` | Fired when HP remains above a threshold (default 50%). |
| `on_low_stamina` | Fired when Stamina drops below 35% (or custom threshold). |
| `on_high_stamina` | Fired when Stamina is above 65%. |
| `on_low_stability` | Fired when Beyblade begins wobbling / low stability. |
| `on_stability_break` | Fired as a last stand right before ring-out / spin finish. |
| `on_gauge_full` | Fired at the moment the Special move gauge fills to 100%. |
| `on_mirror` | Fired when both players pick the same tactical move (Clash / Mirror). |
| `on_attack_win` | Fired when owner wins an Attack clash. |
| `on_defense_win` | Fired when owner successfully blocks / parries with Defense. |
| `on_stamina_win` | Fired when owner out-spins the opponent. |
| `turn_start` | Fired at the beginning of each turn before moves resolve. |
| `turn_end` | Fired after all clashes, damage, and stamina resolutions complete. |
| `setup` | Fired once at battle start to configure stances, mechanics, or modes. |

---

## 3. Conditions & Constraints (`if`, `chance`, `once`, `cooldown`)

Rules inside an ability can contain constraints to govern their execution:

### Chance
```json
"chance": 0.25
```
Evaluated via `random.random() < chance`.

### Frequency / Scope (`once`)
- `"once": "battle"`: Fires only once during the entire battle (e.g. low-HP phase shift, phoenix resurrection).
- `"once": "round"`: Fires once per round.

### Turn Cooldown (`cooldown`)
```json
"cooldown": 3
```
After firing, cannot fire again for 3 rounds.

### Explicit Condition Expressions (`if`)
```json
"if": [
  { "cond": "hp_below_pct", "value": 0.30 }
]
```
Common conditions:
- `hp_below_pct`: Evaluates if current HP ratio $< \text{value}$.
- `hp_above_pct`: Evaluates if current HP ratio $> \text{value}$.
- `stamina_below`: Current stamina $< \text{value}$.
- `stamina_above`: Current stamina $> \text{value}$.
- `winning`: Player has more HP/stamina than opponent.

---

## 4. Mechanical Operations Catalog (`do`)

Operations define what happens when a rule triggers.

### Offensive & Damage Ops
- **`bonus_damage_stat`**: Adds extra damage based on a stat.
  ```json
  { "op": "bonus_damage_stat", "stat": "attack", "scale": 0.3, "label": "Stardust Shockwave" }
  ```
- **`bonus_damage`**: Flat bonus damage.
  ```json
  { "op": "bonus_damage", "value": 25, "label": "Heavy Strike" }
  ```
- **`dmg_amp`**: Amplifies outgoing damage by X% for N turns.
  ```json
  { "op": "dmg_amp", "value": 20, "turns": 2 }
  ```
- **`true_damage`**: Unblockable damage bypassing all defense and shields.
  ```json
  { "op": "true_damage", "value": 30 }
  ```

### Defensive & Sustain Ops
- **`heal`**: Flat HP restoration.
  ```json
  { "op": "heal", "value": 120, "label": "Cosmic Recovery" }
  ```
- **`heal_pct`**: Restores X% of max HP.
  ```json
  { "op": "heal_pct", "value": 15 }
  ```
- **`grant_shield`**: Creates a temporary damage-absorbing barrier.
  ```json
  { "op": "grant_shield", "amount": 80, "turns": 2 }
  ```
- **`damage_reduction`**: Flat or percentage damage reduction.
  ```json
  { "op": "reduce_damage_pct", "value": 25, "turns": 2 }
  ```

### Stat Modification & Glass-Cannon Transformations
- **`stat_transfer`**: Zeroes out or deducts from one stat and transfers those points to another stat.
  ```json
  {
    "op": "stat_transfer",
    "from_stat": "defense",
    "to_stat": "attack",
    "amount": 88,
    "zero_source": true,
    "turns": 99,
    "label": "Event Horizon"
  }
  ```
- **`buff`**: Increases user stat by flat or percentage.
  ```json
  { "op": "buff", "stat": "attack", "pct": 15, "turns": 3 }
  ```

### Debuffs & Status Inflictions
- **`shatter`**: Permanently shreds enemy defense/stat by X%.
  ```json
  {
    "op": "shatter",
    "stat": "defense",
    "pct": 20,
    "turns": 99,
    "label": "Galactic Rift Slash (Shatter)",
    "emoji": "💔"
  }
  ```
- **`enemy_debuff_pct`**: Temporarily reduces enemy stat by X%.
  ```json
  { "op": "enemy_debuff_pct", "stat": "attack", "pct": 15, "turns": 2 }
  ```
- **`burn` / `bleed` / `paralyze`**: Applies status conditions over time.

### Resource & Economy Ops
- **`consume_stamina` / `lose_stamina`**: Costs stamina to fuel powerful effects.
  ```json
  { "op": "consume_stamina", "value": 1.0, "label": "Stamina Cost" }
  ```
- **`drain_stamina`**: Steals stamina from the opponent.
  ```json
  { "op": "drain_stamina", "value": 2.5 }
  ```
- **`gain_gauge`**: Awards Special Move gauge points.
  ```json
  { "op": "gain_gauge", "value": 25 }
  ```

---

## 5. Protocol for Extending AbilityEngine

When an ability introduces a mechanic not yet supported by `AbilityEngine`:

1. **Verify Existing Primitives First**: Check if a combination of existing ops (`stat_transfer`, `bonus_damage_stat`, `shatter`, `dmg_amp`) can express the mechanic accurately without code changes.
2. **If Extending Triggers**:
   - Add the trigger name to `TRIGGERS` set at the top of `cogs/abilities/ability_engine.py`.
   - Dispatch the trigger at the exact execution lifecycle in `AbilityEngine.apply()`, `_fire()`, or `_fire_defensive()`.
3. **If Extending Operations**:
   - Add the new `elif kind in (...)` block inside `AbilityEngine._run_ops()`.
   - Always wrap execution in `try ... except Exception` so rogue data never crashes a battle.
   - Use clean battle log formatting matching the engine standard: `f"{emoji} **{label}** — {message}"`.
4. **Write Unit Tests**: Add test assertions in `tests/` verifying the trigger, operation, and log outputs.
