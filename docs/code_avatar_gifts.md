# Avatar progression in admin gift codes

Admin → Code Create → Add reward → Avatar card opens fields for avatar name/ID, card level (1–5), and stars (1–15). Each reward is configured independently when added; the original preview updates with its level and stars.

The typed reward syntax is `avatar:Tyson@5#8` for level 5 and 8 stars. `avatar:Tyson`, `avatar:Tyson@5`, and `avatar:Tyson#8` also work; omitted values default to 1. Invalid levels/stars reject the whole addition before a code is created. Admin gifts can award stars above the tiers available through random upgrades.

Redemption queues the configured progression in the existing durable Keep/Sell decision. Keep (including timeout Keep) grants those settings on a newly owned avatar. An existing owner receives a duplicate and their owned avatar is raised to at least the gifted level and stars. Higher owned progression, selected skills, feeding credits, and recorded upgrade spending are retained. Sell awards the existing sell value without changing avatar ownership or progression. Gifted levels record no coin spending and therefore create no refundable upgrade cost.

Old codes and pending rewards without progression fields default to level 1 and 1 star. Deterministic code reward IDs keep retries and repeated Keep/Sell clicks from granting multiple copies or applying progression twice.

Validation: `python -m unittest tools.test_code_avatar_progression tools.test_avatar_progression tools.test_avatar_progression_v4`.
