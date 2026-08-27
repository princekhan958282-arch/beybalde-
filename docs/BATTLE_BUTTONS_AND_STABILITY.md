# Battle Buttons & Stability — Technical Reference

Traced directly from the battle engine source. File references point at the
implementation for anyone who wants to verify a number.

Core files:
- `cogs/battle/attack_manager.py` — Attack + Special resolution
- `cogs/battle/defense_manager.py` — Defense resolution, Grind debuff
- `cogs/battle/damage_rules.py` — raw damage formulas
- `cogs/battle/stamina_manager.py` — Stamina resource, the Stamina move, Charge gauge
- `cogs/battle/stability_manager.py` — the Stability resource
- `cogs/battle/special_gate.py` — Special's unlock requirement
- `cogs/core/constants.py` — every numeric constant referenced below

One thing that applies everywhere below: **Attack and Defense stability
costs only apply while a type-advantage bonus is active** for that matchup
(the Attack→Stamina→Defense→Attack triangle, resolved in
`cogs/abilities/type_system.py`). A non-advantaged Attack or Defense still
deals/mitigates damage normally, it just doesn't touch Stability that round.

---

## 1. ⚔️ Attack Button

**How it works:** Attack resolves differently depending on what the
opponent picked that round (`damage_rules.py: calc_damage`):

| Opponent's move | Result |
|---|---|
| Attack (mirror) | Both sides trade damage — a "clash" |
| Defense | Attacker is mitigated by the Shield Gate (see §2) |
| Stamina | Attacker deals **1.2×** damage (a clean win) |
| Charge | Attacker deals **1.5×** damage (a clean win) |

**Stamina cost:** scales with the blade's **Attack stat measured against its
own Stamina stat** — `2.2 × (1 + 0.5 × (attack ÷ stamina − 1))`, clamped to
1.2–4.5. A balanced blade still pays about the old flat `2.2`; a heavy hitter
on a small stamina bar pays more (Blood Dragon, 158 ATK / 81 STA, pays
**3.25**), and a blade with stamina to spare pays less. Because it is a ratio
of two stats that grow together, the cost barely moves with level.

**Stability cost:**
- Hit landed: **-10**
- Hit dealt 0 damage: **-4**
- If the attacker was also hit back that round (e.g. a Defense counter):
  an extra **-5**
- Attack-vs-Attack clash: **-10 to both sides** (this replaces the
  hit/miss cost above, it isn't stacked on top)

**Damage calculation:**
- Base hit = attacker's Attack stat, scaled by the matchup multiplier
  above (1.0 mirror / 1.2 vs Stamina / 1.5 vs Charge), then a **crit
  chance of 10–12%** (scales slightly with Attack stat, capped at 12%)
  can multiply the hit by **1.6×**.
- Every *normal* Attack hit (not Specials, not counters) is then scaled
  down by a flat **×0.5** (`NORMAL_ATTACK_DAMAGE_SCALE`).
- Result is floored at a minimum of 5 damage.
- Against Defense, the raw hit first passes through the Shield Gate
  (§2) before the ×0.5 scale is applied.
- A "deficit bleed" bonus adds extra damage the more the attacker's
  stat outweighs the defender's: `+ (attack_stat − defense_stat) / 200`
  as a multiplier on top.

**Defense interaction:** Attacking into Defense runs the target's
Shield Gate — most of the hit is either largely blocked (if it's under
60% of their Defense stat) or reduced by a flat amount (their Defense
stat × 0.3) if it gets through. A **crit bypasses the block gate** but
still takes the flat reduction. See §2 for the exact numbers.

**Other effects/triggers:**
- Gains the attacker **+15** Special gauge per use, regardless of
  outcome.
- If the hit lands, the defender gains **+12** gauge (dealt-damage
  gauge).
- Fires the `on_attack_hit` / `on_attack_win` / `on_attack_loss` /
  `on_attack_mirror` ability triggers (a blade's authored abilities can
  react to these).
- When Attack-type advantage is active, landing a hit strips **6**
  Stability from the defender (half that, 3, if the defender is a
  Balance-type blade).
- A landed Attack can proc a knockout-chance bonus worth roughly 25% of
  the damage dealt on top.

---

## 2. 🛡️ Defense Button

**How it works:** Defense pre-processes the defender's Defense stat
(applying any active buffs, pierce effects, or debuffs) and then, when
the opponent attacks, runs the incoming hit through the **Shield Gate**:

- If the raw incoming hit is **below 60%** of the defender's Defense
  stat, it's almost fully blocked — only **6%** of the raw hit chips
  through as unavoidable damage.
- Otherwise, the hit is reduced by a **flat amount equal to 30% of the
  defender's Defense stat**, floored at a minimum of 5 damage.

If Defense is used against a **Stamina** move, Defense loses outright:
it deals `defense_stat × 0.5` damage (floored at 5), and it applies a
**Grind debuff** to the Stamina user — draining **1.0 Stamina per
round** from them for a duration based on the defender's Defense stat
(`1 + Defense stat ÷ 50` rounds).

If Defense mirrors another Defense, both sides just trade a flat **32**
chip damage.

**Stamina cost:** scales with the blade's **Defense stat against its own
Stamina stat** — `2.2 × (1 + 0.5 × (defense ÷ stamina − 1))`, clamped to
1.2–4.5. Drakoryn (190 DEF / 88 STA) pays **3.48** to block; Dead Phoenix has
near-identical Defense (189) but far more Stamina (154), so it blocks for
**2.45**. Stamina is the stat that lets a blade actually use its Defense.

**Stability cost/effect:**
- Passive / hit by an advantaged Attack that got through: **-6**
- Mirrored against another Defense: **-3**
- Used against a Stamina move: **0** (no effect)
- Fully blocked the incoming hit (0 damage got through): **+5**

**Damage reduction:** See the Shield Gate numbers above — roughly 94%
of a hit is absorbed below the 60%-of-Defense threshold, otherwise a
flat 30%-of-Defense chunk is shaved off every hit that lands.

**Other effects/triggers:**
- Blocking an attack also fires a **counter hit** back at the
  attacker: `10 + min(50, defense_stat × 0.25)` damage — so it ranges
  from about 10 damage at low Defense up to a 60-damage cap at very
  high Defense.
- Gains the defender **+20** Special gauge per use.
- Fires `on_defense_win` / `on_defense_loss` / `on_defense_mirror` and
  `on_defend` / `on_take_damage` ability triggers.
- When Defense-type advantage is active, a mitigated hit reflects
  **50%** of the mitigated damage straight back at the attacker's own
  HP (halved again if the attacker is Balance-type).

---

## 3. 🌀 Stamina Button

**How much Stamina it restores:**
`3.0 + (Stamina stat × 0.012)`, rounded — plus a flat **+1** bonus if
Stamina-type advantage is active that round. Result is capped at the
blade's own maximum Stamina pool.

**Stability interaction:** Restores **+25** Stability. If the user was
also attacked that same round, the amount is **halved to +12**.

**Limits:**
- Restoration cannot exceed the blade's max Stamina bar.
- Stability restoration cannot push Stability above its starting/max
  value (100, or 150 for Defense-type — see §6).

**Other effects/triggers:**
- Also heals HP: `max(20, ceil(Stamina stat × 0.7 × heal_mult))`,
  where `heal_mult` climbs up to **1.2×** the lower the user's current
  HP is (below 40% HP).
- If the opponent used Attack or Special that same round, the heal is
  **halved**.
- Gains **+10** Special gauge.
- Fires `on_stamina_win` / `on_stamina_loss` / `on_stamina_mirror`
  triggers (Stamina beats Defense, loses to Attack, mirrors anything
  else for trigger purposes).

---

## 4. ⚡ Charge Button

**Charge gained:** A flat **+50** Special gauge per use.

**Stamina cost:** `1.5` per use.

**Charge stacks (opt-in per blade):** a blade whose data authors
`button_profile.charge` banks a **stack** each time it charges, up to its own
cap. Stacks add a percentage to the damage of the **next** Attack or Special
and are then spent. A multi-hit Special spends the whole bank once, on its
first hit — not once per hit.

**Stability interaction:** none by default. A blade that authors
`stability_per_stack` steadies itself slightly for each stack banked.

**The gamble:** if the charging blade is **hit** before it cashes in, the
whole bank is knocked loose. Charging is still the move an opponent most
wants to catch you on (see the 1.5× below), so the stacks are what the
exposure buys — if it survives.

**Limits:** Gauge is capped at **150**. Stack caps are per blade.

**Other effects/triggers:** fires the `on_charge` ability trigger. Note the
move × result matrix nominally spells `on_charge_win`/`on_charge_loss`, but
`calc_damage` classifies **every** Charge as `mirror`, so those two can never
fire — `on_charge` is the hook that actually works. For matchup purposes, an
opponent Attacking into a Charge still counts as the attacker's clean-win case
(1.5× damage, see §1).

---

## 5. ⭐ Special Button

**Charge required:** The gauge must reach the blade's own **gauge cost**,
which is the full **150** unless its data authors
`button_profile.special.gauge_cost`. A cheaper Special fires sooner and leaves
the change on the bar rather than zeroing it. Some blades layer an additional authored requirement on top
(checked in `special_gate.py`):
- A **counter** requirement (e.g. a blade's own charge-up mechanic must
  reach a set value), or
- A **cooldown** requirement (the blade's Special must not still be on
  cooldown from a previous use).

Most blades have no extra requirement — gauge being full is enough.
After Special is used, the gauge is consumed back to 0.

**Stamina cost:** `4.4` per use.

**Stability cost/effect:** **zero by default** — a deliberate choice, since
Specials once cost -10 and a blade could ring *itself* out casting its own
move. A blade may author `button_profile.special.stability_cost`, and it is
clamped to always leave at least 1 Stability: a drawback must never become a
suicide button.

**Damage/effects:** Each blade authors its own Special (hit count,
per-hit damage, and whether it ignores Defense). On top of the
authored numbers:
- Damage scales with the blade's **current Special stat vs. its
  printed Special stat**, up to a hard cap of **2.5×** the printed
  value — leveling a blade's Special stat makes the move hit harder,
  up to that ceiling.
- A minimum per-hit damage floor can be authored per blade.
- If a blade has no authored Special at all, it falls back to a
  single hit worth **60% of the base HP pool (2000)** — 1,200 damage
  before level scaling.
- Landing hits builds "amp" for follow-up hits within the same
  barrage on certain blades, and can trigger authored per-hit bonus
  effects (e.g. bonus damage scaled by the user's own leftover Stamina,
  or by how much Stability the target has already lost).

**Defense interaction:** Special damage is still reduced by the
defender's Defense-type mitigation **unless** the blade's Special is
authored to ignore Defense, or the blade has a partial "pierce"
property that shaves down how much of the mitigation applies.

**Other effects/triggers:**
- The attacker gains **no** gauge from using their own Special.
- The defender gains **+12** gauge for every hit of the barrage that
  lands on them.
- Fires the `on_special` trigger once (on the first hit) and `on_hit`
  for every individual hit in a multi-hit Special.

---

## 6. 🔵 Stability System

**Maximum Stability:** Equal to the blade's **starting** Stability —
whatever that value is, it also acts as the hard ceiling; no
recovery effect can push Stability above it.

**Starting Stability:**
- **100** for Attack, Stamina, and Balance type blades.
- **150** for Defense type blades.

(A player's equipped avatar can add a further starting bonus on top of
these base numbers.)

**Stability consumed by each button:**

| Button | Cost | Notes |
|---|---|---|
| ⚔️ Attack | **-10** hit / **-4** miss (extra **-5** if countered) | Attack-vs-Attack clash is **-10 to both** instead, replacing hit/miss |
| 🛡️ Defense | **-6** passive/hit, **-3** mirror, **0** vs Stamina, **+5** if fully blocked | |
| 🌀 Stamina | **+25** (restores) | Halved to **+12** if the user was attacked that round |
| ⚡ Charge | **0** (or a small gain, if the blade authors `stability_per_stack`) | |
| ⭐ Special | **0** | Explicit design decision — Special never touches Stability |

Attack and Defense costs only apply while type-advantage effects are
active for that matchup (see the note at the top of this document) —
a non-advantaged Attack/Defense still deals/mitigates damage as usual,
it just skips the Stability change that round.

**Stability recovery:** The only recovery path is the Stamina button
(+25, or +12 if the user was hit that round). Defense can also gain a
small +5 when it fully blocks a hit. There is no passive per-round
regeneration outside of using these moves.

**What happens when Stability reaches 0:** It is checked at three
points every round. The moment a blade's Stability drops to 0 or below,
**that blade's HP is immediately forced to 0 and the battle ends** on
the spot as a ring-out loss — regardless of how much HP it actually had
left.

**The gradient (opt-in).** By default that cliff is the *whole* story: a blade
at 1 Stability plays exactly like one at full, and then 0 ends the fight. A
blade whose data authors `button_profile.stability.tiers` (or a global
`STABILITY_TIERS`) instead takes **more damage as its bar falls**, in bands —
so the descent itself matters and spending a turn on Stamina becomes a real
defensive play rather than something you only regret not doing. When several
bands apply, the harshest one wins.

**How Stability affects each move:**
- **Attack:** Costs Stability to use (when advantaged); doesn't change
  Attack's own damage output. Reaching 0 Stability (on either side)
  ends the fight instantly regardless of what happens in the Attack
  exchange.
- **Defense:** Costs or refunds Stability depending on the outcome
  (see table above); doesn't change Defense's own mitigation math.
- **Stamina:** Its sole Stability role is as the main *recovery* tool
  — it's the only move that restores Stability by a meaningful amount.
- **Charge:** No interaction whatsoever.
- **Special:** No interaction whatsoever — using or receiving a
  Special never changes Stability directly.

**Additional effects caused by Stability:**
- Some blade abilities carry a **burst resistance** property that
  reduces the size of a negative Stability hit (it can shrink a
  penalty but never fully cancel it — every Stability loss is at least
  -1).
- One specific Special-move bonus effect scales its bonus damage off
  *how much Stability the target has already lost* — the more
  Stability an opponent has burned through, the harder that particular
  bonus hits. This is the only place a live Stability value feeds back
  into a damage number; everywhere else, Stability only gates whether
  a cost applies and whether the ring-out check fires.
