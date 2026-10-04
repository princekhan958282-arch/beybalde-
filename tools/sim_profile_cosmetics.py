"""Targeted checks for the 90K profile cosmetic feature.

Run: python tools/sim_profile_cosmetics.py
"""
from utils.profile_cosmetics import (
    PROFILE_COSMETICS, DEFAULT_PROFILE_THEME, ProfileCosmeticError,
    apply_profile_purchase, apply_profile_equip, equipped_theme, owned_themes,
)

passed = 0

def check(label, cond):
    global passed
    if not cond:
        raise AssertionError(label)
    passed += 1
    print("PASS", label)

item = PROFILE_COSMETICS["cyber_arena"]
check("price is exactly 90,000", item["price"] == 90_000)

p = {"coins": 100_000}
r = apply_profile_purchase(p, "cyber_arena")
check("purchase charges 90K", r["spent"] == 90_000 and p["coins"] == 10_000)
check("purchase permanently unlocks", "cyber_arena" in p["owned_profile_themes"])
check("purchase auto-equips", equipped_theme(p) == "cyber_arena")
check("default always remains switchable", DEFAULT_PROFILE_THEME in owned_themes(p))

apply_profile_equip(p, "default")
check("can switch back to default", equipped_theme(p) == "default")
apply_profile_equip(p, "cyber_arena")
check("can re-equip purchased theme", equipped_theme(p) == "cyber_arena")

before = dict(p)
try:
    apply_profile_purchase(p, "cyber_arena")
    raise AssertionError("duplicate purchase was accepted")
except ProfileCosmeticError:
    pass
check("duplicate purchase does not charge", p["coins"] == before["coins"])

poor = {"coins": 89_999}
try:
    apply_profile_purchase(poor, "cyber_arena")
    raise AssertionError("underfunded purchase was accepted")
except ProfileCosmeticError:
    pass
check("insufficient funds do not charge", poor["coins"] == 89_999)

fresh = {"coins": 0}
try:
    apply_profile_equip(fresh, "cyber_arena")
    raise AssertionError("unowned theme equipped")
except ProfileCosmeticError:
    pass
check("unowned theme cannot equip", equipped_theme(fresh) == "default")

print(f"\n{passed} profile cosmetic checks passed.")
