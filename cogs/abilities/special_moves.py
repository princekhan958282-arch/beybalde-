"""
cogs/abilities/special_moves.py — generic stub (v2 engine)
----------------------------------------------------------
The bespoke per-blade special handlers were removed with the hardcoded
ability engine.  All Special effects are now expressed as rules with
`"when": "on_special"` in beyblades.json and interpreted generically by
AbilityEngine.  Every Special therefore runs through the standard
multi-hit loop in AttackManager — no blade manages its own HP writes.
"""
from __future__ import annotations

# No blade self-manages its hits anymore; the standard loop handles all.
SELF_MANAGED_HITS: frozenset[str] = frozenset()
