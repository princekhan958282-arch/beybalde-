PvP, ranked/tournament battles and Story now start from the equipped Bey's resolved HP stat multiplied by 15, with no fixed base HP. A stat of 100 gives 1500 HP before existing avatar HP boosts and type passives. Each HP point from level growth or equipped components also contributes 15 battle HP. Negative component adjustments reduce the pool.

HP reads no longer clamp authored or levelled values into a type band. Type bands remain the fallback for missing authored HP and the display bar scale. A zero or depleted loadout has a minimum battle pool of one HP.

Avatar flat HP and percentage bonuses apply once after conversion, followed by existing type passives. Story opponents already carry their levelled HP stat, so the controller's legacy HP gain is not added again. Displayed owned battle pools use the same order as the session.

Chain thresholds and percentage healing use the fighter's own maximum. Stamina recovery respects the converted maximum, and a blade without an authored Special uses its own converted pool for fallback damage.

Boss HP remains on its existing independent calculation. No Boss implementation files or Boss HP constants are changed. The shared regression suite retains explicit checks of the original Boss player HP formula.

Validation includes direct 15:1 conversion, levels, equipped components, negative component contributions, Story NPC levelling, avatar application and card parity, healing caps, Special fallback, and existing Boss formula assertions. The broader number simulation has a pre-existing Special-growth assertion failure (118 of 127 damaging Specials grow); it reproduces without these HP changes.
