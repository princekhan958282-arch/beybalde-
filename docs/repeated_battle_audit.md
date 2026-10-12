# Repeated battle and ability audit

Base inspected: `9acabcca08b890b7c7889d0f83b9c1399d65f6f8` (latest main when the audit began).
PR #255 was already merged. Its Wind Knight full-pierce fix, silence dispatch,
critical-gauge commit and recoil deduplication are retained and regression-tested.
Final integration base: `620a0256f6262ec31829008b98bf941c4971a228`. PR #256
(Requiem Whip/Zone) merged during the audit; its implementation and six live-round
regressions are preserved and included in final verification.
The only open PR returned by the initial repository query was #252 (custom image assets).

## Confirmed bugs and corrections

| Bug / affected system | Real-round reproduction | Before | After |
|---|---|---|---|
| Amplified partial pierce: Janus Bahamut, Attack form | Lv100 Janus versus Noctilune: four Attack/Defense losses, Gatecleaver against Charge, then Attack against Charge | Gatecleaver doubles 50% pierce to 100%; DEF becomes zero. The sixth round removes **4,375 actual HP**, killing Noctilune. | Partial pierce approaches the same neutral stat-ratio endpoint as PR #255's full pierce. Sixth-round damage is **244 HP**. Ordinary favorable ATK/DEF ratios remain valid. |
| Multi-hit move events: Master Diabolos, Aetherion Drakos org and shared passive/result/threshold dispatch | Use one authored multi-hit Special against Charge at Lv1 | Master gains two win stacks from one exchange; Aetherion banks all six Stars in one round. Periodic healing, drains and threshold grants also dispatch per hit. | One Master stack / one Star. Move-result, threshold and periodic state effects run once per move; passive damage riders still modify each hit. Explicit `on_hit` remains per hit. |
| Premature lifesteal: Cobalt Dragoon; shared `lifesteal_pct`, custom Life Steal and Artemis's rate storage | Injure Cobalt, then Attack an avatar with forced dodge. Also attack an opponent with only 5 HP left. | Zero committed damage still heals 6 HP in the dodge case. Healing can use damage exceeding the opponent's remaining HP. | Dodge heals zero. Overkill healing uses the **5 actual HP removed** (1 HP at Cobalt's authored rate). Both simultaneous hits commit before ordinary lifesteal heals; lifesteal does not revive a defeated Bey. |
| Premature successful-hit state: Blood Dragon, Void Longinus, Radiant Valkyrie, Azure Drakonyx, Guilty Longinus and shared hit DSL | Repeat four shielded, Kinetic-nullified or avatar-dodged attacks; repeat dodged multi-hit Specials. Then allow real hits. | Blood banks +8 ATK and both cost surcharges on a zero-damage hit; other hit riders can grant stability, drains, counters, debuffs or consume cooldowns despite dodge. | Successful-hit state is committed only for hits contributing actual HP damage. Temporary counter projections preserve authored third-hit payoff ordering, without granting real stacks on failure. Blood still reaches its authored 10-stack/+80 ATK ceiling; Radiant Rush's cooldown remains available after dodge. |
| Premature avatar nth-strike progress | Force four avatar dodges, then permit two attacks with an every-second-strike avatar fixture | Dodged attacks advance the strike count. | Count remains zero during dodge; first landed attack counts one, second counts two and gains the rider. The rider announcement appears only on a landed hit. |
| Damage-triggered reflection: Twin Nemesis and timed reflect windows | Attack a dodging Twin Nemesis avatar; run an Attack mirror with 50% reflect windows on both sides | Twin returns 16 damage even though it took none. Mirror checks discard valid reflections; a mutual-exclusion safeguard can drop one side's authored return. | Dodge returns zero. Each mirror side reflects its own authored fraction of committed incoming HP damage. Reflection logs report actual HP removed. Kinetic Counter's exact return still replaces other returned damage. Attempt-based `on_defend` rules retain their distinct contract. |
| Premature damage-taken gauge: all Attack / multi-hit Special paths | Blood Dragon attacks or uses Blood Claw into avatar dodge while the defender Charges | Defender receives damage-taken gauge despite losing no HP (including one award per projected Special hit). | Defender receives its Charge gauge only. Damage-taken gauge is awarded only for hit portions contributing actual HP damage. Attack/Defense action gauge is retained. |
| Passive property reader bypasses timing and silence: Janus, Cure Black, shared pierce/counter property readers | Silence Janus before Attack; inspect Cure Black before using Black Cure | Silenced Janus retains passive pierce. Cure's `on_special` 8% pierce is read as an unconditional normal-attack passive. | Passive readers honor silence, rule conditions and ability disabling. Triggered Special pierce is routed to that Special's mitigation instead of becoming a permanent passive. |
| Ignored rule-level cooldowns: Cure Black / Cure White | Refill the test gauge and use the Special for five consecutive real rounds | Black heals `[8,8,8,8,8]`; White heals every round too. Authored `cooldown: 4` is ignored. | Black heals `[8,0,0,0,8]`; White `[12,0,0,0,12]`. Cooldowns are battle-local and scoped to the compiled form/rule. |
| Inactive adapters change rounding: all real-session attacks | Lv100 Victory Valkyrie versus Blood Dragon, no attacker abilities; force a natural type critical against Charge | Original Generation truncates before the crit, and an inactive Burst guard rounds damage upward. This fixture deals 808 instead of `floor(2 × raw)` = 809. | Inactive adapters preserve formula precision. The fixture deals 809, with final HP rounding at commit. Active authored effects retain their own rounding. |
| Special log ambiguity: all multi-hit Specials | Use Blood Claw into avatar dodge | Per-hit / total Special lines print projected damage as though it were actual HP loss, while the move summary correctly says zero. | Per-hit and Special totals explicitly say **before avatar defenses**. Main move summaries continue to show committed HP loss; counter and healing logs use actual amounts. |

The Janus spike requires its Special amplification window. Four losses alone do
not activate that window. The original player report did not include enough
loadout/history data to identify the exact Bey involved; Wind Knight's related
three-successful-defenses spike was already fixed in merged PR #255.
No universal damage cap, stat denominator floor added to the shared formula,
blanket nerf, or authored Bey stat changes were used.

## Evidence and coverage

`repeated_battle_audit_traces.json` records before/after damage, real formula
stat snapshots, HP, counters, buffs/durations, cooldowns, avatar strike counts,
stamina, stability, gauge and rendered result text. Baseline traces execute the
same harness against the untouched main commit above. The trace harness observes
`resolve_pair` arguments; it does not replace combat arithmetic.

The reproducible sweep exercises **169 official Beys and 12 additional mounted
forms**, levels 1 and 100, both player orders, four Attack-versus-Defense rounds,
then Attack or Special: **1,448 completed five-round sessions**, zero execution
errors. The opponent is Noctilune with avatars off and default equipment. Both
baseline and candidate sweeps have a maximum committed hit of 1,209 HP in these
particular loss-only sequences. The separate Janus Special-then-Attack reproduction
is required to expose the 4,375 HP spike.

Targeted regressions additionally cross levels 1/100, both seats, avatars on/off
and default versus genuinely equipped Destroy Drivers for Janus and Custom Bey
loss-trigger growth/shatter. Avatar-on cases use the real ownership/skill pipeline;
guaranteed dodge, nth-strike and once-hit contract cases use controlled fixtures.
Master's and Aetherion's move/round stack tests distinguish them from Blood's
intentionally per-hit stacks. Guilty and Azure retain their third-hit payoffs.
Custom loss growth respects the current balanced +3-per-stack preset, four-round
cooldown and temporary defense-shatter lifetime; no denominator collapse was
reproduced for those selected presets. Stock part replacements are applied once
through the physical component resolver. Legacy shatter preprocessing still
exists, but this registry has no authored legacy shatter kit to reproduce through
the public roster; no speculative balance change was made to it.

To finish repeated sequences, fixtures reserve stamina/stability; the long Blood
stack-cap and sweep cases refill stamina when needed. Effective Bey stats and
natural HP pools are retained. Special test gauges are refilled to exercise
cooldowns independently of charging time. These are controlled integration
reproductions, not claims about naturally attainable consecutive Specials.

Commands:

```sh
python -m unittest tools.test_repeated_battle_audit
python -m tools.audit_repeated_battles --out /tmp/battle-audit.json --all
```

## Regression results and existing failures

- **615 unittest executions passed; 5 live-MySQL cases skipped locally** because
  no MySQL test URL was configured (620 total across 34 isolated commands).
- The duration and type simulations passed **134 additional checks**.
- The 1,448-session sweep completed without execution errors.
- Existing Avatar/ability workflow commands, combat rework, blocked-hit/silence,
  full-pierce, Void/Noctilune, original avatars, Draciel/Dranzer, Boss flow/revamp,
  physical components, Custom Bey balance, Avatar V4 SQLite and Kirindael Domain
  regression suites passed. New session regressions are included in CI.
- Python compilation and `git diff --check` passed.

Two combat-rework tests failed on untouched latest main: Special guard expected
172 rather than 68 HP; avatar Special riders expected 520 rather than 208 HP.
Both omitted the already-authored 60% Defense-versus-Special guard. The Kinetic
expectation similarly needs 48 instead of 120. Expectations now match the
documented guard order, without changing the guard itself. The older critical
tolerance test passed on latest main, but the new exact 808/809 fixture exposed
the remaining inactive-adapter precision error, which is fixed.

The Kirindael Domain healing test needed to commit HP before asserting lifesteal
after the timing correction; its 25% healing reduction still passes. Isolated
ability-dispatch fixtures remain useful for authored operation coverage, while
the new regressions establish live-round behavior and successful-hit timing.

## Remaining limits

- Exact live player incident/loadout remains unidentified. The reproducible
  Janus and merged Wind Knight causes are concrete alternatives, not proof of
  which Bey was in that player's match.
- This is not exhaustive coverage of every Bey-versus-Bey pairing, every
  avatar/star/skill combination, all part permutations, or player-authored
  historical custom data. The broad sweep uses one opponent; targeted matrices
  cover the relevant systems and controlled blocked-hit cases.
- The separate Boss resolver has its own extracted `BladeKit` dialect and
  adapters. Existing Boss regressions pass, but a full behavioral parity audit
  of all Boss state machines versus BattleSession remains unresolved.
- Defensive state grants under `on_take_damage` that also participate in
  mitigation (for example resistance-stack buildup), direct terminal-damage
  ability log totals, and legacy `_chain` hit-rule transactions warrant a
  separate focused audit. This change defers hit-rule rewards and damage-based
  reflect returns; it does not transactionally rewrite every defensive hook.
- Local live MySQL verification is deferred to the CI service-backed checks.

No merge or live Discord deployment was performed.
