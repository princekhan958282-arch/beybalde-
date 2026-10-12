# Noctilune battle effects

Lunar Blossom now has executable rules instead of description-only metadata.

- Each lost Attack/Defense/Stamina clash grants one Lunar Petal, capped at five. Wins and mirrors grant none.
- Each petal adds 4% of the battle blade's ATK and DEF, rounded to whole stat points. Both bonuses share one counter and source.
- Reaching five petals triggers Full Bloom if fewer than two blooms have activated: heal 15% max HP, cleanse one temporary negative effect, reset petals and remove their ATK/DEF bonuses. Unrelated buffs remain.
- After the second Full Bloom, further petals remain banked at the five-petal cap.
- Midnight Butterfly deals 150 + 45% of current effective ATK. Petals add 6% damage each without consumption. At five petals, the Special gains 20% partial defense pierce; the Defense button still guards the hit.

The generic stacking operation accepts an optional stat list, preserving the single-stat default. The generic stack damage operation accepts `consume: false`, preserving consumption by default. Existing identity, base stats, component totals, art, rarity and ownership data remain unchanged. The original component-migration baseline is retained; a separately documented correction pins the authorized kit change.

Verification: `python -m unittest tools.test_noctilune`. Covers real losses, stack cap, dual-stat gains, two blooms, targeted cleanup, exact cleanse count, live ATK formula, retained petals, conditional partial pierce and legacy consumption.
