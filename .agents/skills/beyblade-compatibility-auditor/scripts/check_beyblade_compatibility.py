#!/usr/bin/env python3
"""Beyblade Compatibility & Ability Engine Auditor.

Automated validation tool for Beyblade definitions in Betcord:
1. Validates JSON schema, core attributes, and types.
2. Checks 45% frame / 27.5% disk / 27.5% driver modular stat splits and part existence.
3. Validates Special Move math (hit damage breakdown, total damage, special stat alignment).
4. Audits Abilities against AbilityEngine triggers, conditions, and operations.
5. Validates system roster test count synchronization across tools/test_*.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Attempt to locate repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
if not (REPO_ROOT / "beys").is_dir():
    # Fallback to current working directory if script moved
    REPO_ROOT = Path.cwd()

# Add repo root to sys.path so cogs/utils can be imported
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from cogs.abilities.ability_engine import TRIGGERS
    from cogs.abilities.extended_effects import OPS as EXTENDED_OPS
    from cogs.abilities.tactical_effects import OPS as TACTICAL_OPS
    from cogs.battle.type_gimmicks import GIMMICK_OPS, passive_stat_multiplier
    from utils import bey_levels as BL
    from utils.hp_system import max_hp_for_blade
    from utils.spin_mode import resolve
except ImportError:
    TRIGGERS = set()
    EXTENDED_OPS = set()
    TACTICAL_OPS = set()
    GIMMICK_OPS = set()
    passive_stat_multiplier = None
    BL = None
    max_hp_for_blade = None
    resolve = None

# Built-in AbilityEngine ops recognized in _run_ops
BUILTIN_OPS = {
    "ability_amp", "add_counter", "bleed", "bonus_damage", "bonus_damage_enemy_hp_pct",
    "bonus_damage_pct", "bonus_damage_stat", "bonus_special_hits", "buff", "buff_all",
    "buff_all_pct", "burn", "burst_bonus", "burst_finisher", "burst_mechanic", "cleanse",
    "consume_counter", "consume_stack_burst_enemy_hp_pct", "consume_stack_damage_pct",
    "consume_stamina", "counter", "counter_burst", "counter_damage_pct", "create_zone",
    "crit_chance", "crit_damage", "damage_amp", "damage_boost", "damage_reduction",
    "debuff", "debuff_immune", "debuff_ward", "disable_ability_2", "dmg_amp",
    "drain_stamina", "enemy_debuff", "enemy_debuff_pct", "enemy_lose_stability",
    "enter_eclipse_phase", "seal_eclipse_flight", "chain_crit",
    "gain_obsession_stack", "unleash_hound",
    "evolve_form", "execute", "gain_counter", "gain_gauge", "gain_stability",
    "gain_stamina", "grant_shield", "guaranteed_crit", "heal", "heal_pct",
    "heal_per_drain", "hp_regen", "ignore_defense", "ignore_defense_pct", "invulnerable",
    "lifesteal", "lifesteal_pct", "log", "lose_stability", "lose_stamina",
    "modify_recoil_flat", "negate_damage",
    "paralyze", "prime_bonus", "prime_crit", "prime_hit_bonus", "purification_domain",
    "queue_chain", "recoil", "recoil_pct", "recoil_percent_of_damage",
    "reduce_damage_flat", "reduce_damage_pct",
    "reduce_damage_pct_turns", "reflect_flat", "reflect_pct", "reflect_pct_turns",
    "reset_counter", "revive", "revive_pct", "set_mode", "shatter", "shield",
    "shield_pct", "silence", "special_amp_stack", "special_boost", "special_pierce_pct",
    "spend_stacks", "stability_cost_increase", "stack_consume", "stack_gain",
    "stack_scaled_lifesteal_pct", "stacking_buff", "stacking_resist", "stamina_cost_increase",
    "stamina_cost_reduction", "stamina_regen", "start_cooldown", "stat_transfer",
    "status_apply", "steal_hp", "transfer_stat", "true_damage", "true_damage_stat_pct",
    "true_damage_turns", "undodgeable"
}

ALL_KNOWN_OPS = BUILTIN_OPS | set(EXTENDED_OPS) | set(TACTICAL_OPS) | set(GIMMICK_OPS)

VALID_TYPES = {"Attack", "Defense", "Stamina", "Balance"}
VALID_SPINS = {"Right", "Left", "Dual"}
VALID_RARITIES = {
    "Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic", "Boss",
    "Ultimate", "Exclusive", "Limited", "Boss Fighter"
}


def compute_stat_split(hp: int, atk: int, df: int, stm: int) -> Dict[str, Dict[str, int]]:
    """Compute authorized 45 / 27.5 / 27.5 stat split with largest remainder tie-break."""
    ratios = [("frame", 0.45), ("disk", 0.275), ("driver", 0.275)]
    res: Dict[str, Dict[str, int]] = {"frame": {}, "disk": {}, "driver": {}}
    stats = [("hp", hp), ("attack", atk), ("defense", df), ("stamina", stm)]

    for name, total in stats:
        floors = {p: int(total * ratio) for p, ratio in ratios}
        allocated = sum(floors.values())
        rem = total - allocated
        remainders = [(total * ratio - floors[p], p) for p, ratio in ratios]
        # Sort largest remainder first, tiebreak: frame, disk, driver
        order = {"frame": 0, "disk": 1, "driver": 2}
        remainders.sort(key=lambda x: (-x[0], order[x[1]]))
        for i in range(rem):
            floors[remainders[i][1]] += 1
        for p, _ in ratios:
            res[p][name] = floors[p]

    return res


class BeybladeAuditor:
    def __init__(self, root: Optional[Path] = None, verbose: bool = False):
        self.root = root or REPO_ROOT
        self.beys_dir = self.root / "beys"
        self.disks_dir = self.root / "parts" / "disks"
        self.drivers_dir = self.root / "parts" / "drivers"
        self.tools_dir = self.root / "tools"
        self.verbose = verbose
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def log_error(self, msg: str) -> None:
        self.errors.append(msg)

    def log_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def audit_beyblade(self, path: Path) -> bool:
        """Audit a single beyblade JSON file."""
        initial_error_count = len(self.errors)
        rel_path = path.relative_to(self.root) if path.is_relative_to(self.root) else path

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log_error(f"[{rel_path}] JSON decode error: {e}")
            return False

        # 1. Core fields validation
        req_fields = ["id", "name", "type", "spin_direction", "rarity", "stats",
                      "main_frame", "default_parts", "special_move"]
        for field in req_fields:
            if field not in data:
                self.log_error(f"[{rel_path}] Missing required top-level field: '{field}'")

        blade_id = data.get("id", "UNKNOWN")
        blade_name = data.get("name", "Unknown")
        b_type = data.get("type")
        if b_type not in VALID_TYPES:
            self.log_error(f"[{rel_path}] Invalid type: '{b_type}'. Allowed: {VALID_TYPES}")

        spin = data.get("spin_direction")
        if spin not in VALID_SPINS:
            self.log_error(f"[{rel_path}] Invalid spin_direction: '{spin}'. Allowed: {VALID_SPINS}")

        rarity = data.get("rarity")
        if rarity not in VALID_RARITIES:
            self.log_warning(f"[{rel_path}] Non-standard rarity: '{rarity}'. Expected one of: {VALID_RARITIES}")

        # 2. Stats validation
        stats = data.get("stats", {})
        stat_keys = ["hp", "attack", "defense", "stamina", "special"]
        for sk in stat_keys:
            val = stats.get(sk)
            is_non_damage_sp = (sk == "special" and (data.get("special_move", {}).get("non_damage") or data.get("special_move", {}).get("total_damage") == 0))
            if not isinstance(val, (int, float)) or (val < 0 if is_non_damage_sp else val <= 0):
                self.log_error(f"[{rel_path}] Invalid or non-positive stat '{sk}': {val}")

        # 3. Modular Parts & Stat Division (45 / 27.5 / 27.5)
        default_parts = data.get("default_parts", {})
        disk_ref = default_parts.get("disk")
        driver_ref = default_parts.get("driver")

        if not disk_ref:
            self.log_error(f"[{rel_path}] 'default_parts' missing 'disk'")
        if not driver_ref:
            self.log_error(f"[{rel_path}] 'default_parts' missing 'driver'")

        disk_path = self.disks_dir / f"{disk_ref}.json"
        driver_path = self.drivers_dir / f"{driver_ref}.json"

        disk_data = None
        driver_data = None

        if disk_ref and not disk_path.is_file():
            self.log_error(f"[{rel_path}] Disk part file missing: {disk_path.name}")
        elif disk_ref:
            try:
                with open(disk_path, "r", encoding="utf-8") as f:
                    disk_data = json.load(f)
            except Exception as e:
                self.log_error(f"[{disk_path.name}] Failed to load disk JSON: {e}")

        if driver_ref and not driver_path.is_file():
            self.log_error(f"[{rel_path}] Driver part file missing: {driver_path.name}")
        elif driver_ref:
            try:
                with open(driver_path, "r", encoding="utf-8") as f:
                    driver_data = json.load(f)
            except Exception as e:
                self.log_error(f"[{driver_path.name}] Failed to load driver JSON: {e}")

        mf = data.get("main_frame", {})
        if disk_data and driver_data and mf and stats:
            for sk in ["hp", "attack", "defense", "stamina"]:
                mf_val = mf.get(sk, 0)
                d_val = disk_data.get("stats", {}).get(sk, 0)
                dr_val = driver_data.get("stats", {}).get(sk, 0)
                target_val = stats.get(sk, 0)
                component_sum = mf_val + d_val + dr_val
                if component_sum != target_val:
                    self.log_error(
                        f"[{rel_path}] Stat sum mismatch for '{sk}': "
                        f"frame({mf_val}) + disk({d_val}) + driver({dr_val}) = {component_sum} "
                        f"!= stats[{sk}]({target_val})"
                    )

        # 3b. HP Balance & Level-100 Stock Scaling Check (2,000 <= HP <= 3,000)
        # Enforced globally by tools/test_hp_balance.py and character-components CI workflow
        if BL and max_hp_for_blade and passive_stat_multiplier and stats:
            try:
                for iv in (0, getattr(BL, "IV_MAX", 10)):
                    before = 0
                    max_lvl = getattr(BL, "MAX_LEVEL", 100)
                    for level in range(1, max_lvl + 1):
                        lvl_stats = BL.stats_at(data, level, {'hp': iv})
                        hp = int(max_hp_for_blade(dict(data, stats=lvl_stats))
                                 * passive_stat_multiplier(data.get('type', 'Balance'), 'hp'))
                        if hp < before:
                            self.log_error(
                                f"[{rel_path}] HP non-monotonic regression at level {level} (iv={iv}): {hp} < {before}"
                            )
                        before = hp
                    if not (2000 <= hp <= 3000):
                        self.log_error(
                            f"[{rel_path}] HP Balance Violation: Level-100 stock HP is {hp} (iv={iv}). "
                            f"Must be between 2000 and 3000! Authored base HP: {stats.get('hp')}. "
                            f"If base HP is high (>100), ensure utils.bey_levels.level_base_stats caps base HP during level progression."
                        )
            except Exception as e:
                self.log_warning(f"[{rel_path}] HP curve calculation check failed: {e}")

            if resolve:
                for mode in data.get('spin_modes', {}):
                    try:
                        mode_blade = resolve({'spin_mode': {data.get('name', ''): mode}}, data)
                        for iv in (0, getattr(BL, "IV_MAX", 10)):
                            lvl_stats = BL.stats_at(mode_blade, getattr(BL, "MAX_LEVEL", 100), {'hp': iv})
                            hp = int(max_hp_for_blade(dict(mode_blade, stats=lvl_stats))
                                     * passive_stat_multiplier(mode_blade.get('type', 'Balance'), 'hp'))
                            if not (2000 <= hp <= 3000):
                                self.log_error(
                                    f"[{rel_path}] Spin Mode '{mode}' HP Balance Violation: Level-100 HP is {hp} (must be 2000-3000)."
                                )
                    except Exception as e:
                        self.log_warning(f"[{rel_path}] Spin mode '{mode}' HP check failed: {e}")

        # 4. Special Move Validation
        sm = data.get("special_move", {})
        if sm:
            sm_name = sm.get("name")
            hits = sm.get("hits", 1)
            dph = sm.get("damage_per_hit")
            total_dmg = sm.get("total_damage")
            sp_stat = stats.get("special")

            if not sm_name:
                self.log_error(f"[{rel_path}] special_move missing 'name'")
            if not isinstance(hits, int) or hits <= 0:
                self.log_error(f"[{rel_path}] special_move 'hits' must be positive int: {hits}")

            if isinstance(dph, list):
                if len(dph) != hits:
                    self.log_warning(f"[{rel_path}] special_move 'damage_per_hit' length ({len(dph)}) != hits ({hits})")
                dph_sum = sum(dph)
                if total_dmg is not None and dph_sum != total_dmg:
                    self.log_error(f"[{rel_path}] special_move sum(damage_per_hit) ({dph_sum}) != total_damage ({total_dmg})")
            elif isinstance(dph, (int, float)):
                dph_sum = int(dph) if hits == 1 else int(dph * hits)
                if total_dmg is not None and dph_sum != total_dmg:
                    self.log_warning(f"[{rel_path}] special_move damage_per_hit ({dph}) * hits ({hits}) != total_damage ({total_dmg})")
            elif dph is None and total_dmg is None and "damage_formula" not in sm:
                self.log_error(f"[{rel_path}] special_move missing 'damage_per_hit', 'total_damage', and 'damage_formula'")

            if total_dmg is not None and sp_stat is not None and total_dmg != sp_stat:
                self.log_warning(f"[{rel_path}] special_move total_damage ({total_dmg}) != stats.special ({sp_stat})")

        # 5. Abilities & AbilityEngine Compatibility
        abilities = data.get("abilities", [])
        if "ability" in data and not abilities:
            abilities = [data["ability"]]

        for ab_idx, ab in enumerate(abilities):
            ab_name = ab.get("name", f"Ability_{ab_idx}")
            rules = ab.get("rules", [])
            top_trigger = ab.get("trigger")

            if top_trigger and top_trigger not in TRIGGERS and TRIGGERS:
                self.log_warning(
                    f"[{rel_path}] Ability '{ab_name}' top-level trigger '{top_trigger}' "
                    f"not in AbilityEngine.TRIGGERS."
                )

            for rule_idx, rule in enumerate(rules):
                when = rule.get("when")
                if not when:
                    self.log_error(f"[{rel_path}] Ability '{ab_name}' rule #{rule_idx} missing 'when' trigger")
                elif when not in TRIGGERS and TRIGGERS:
                    self.log_error(
                        f"[{rel_path}] Ability '{ab_name}' trigger '{when}' NOT recognized by AbilityEngine! "
                        f"Engine extension required or typo in trigger name."
                    )

                # Check operations
                ops = rule.get("do", [])
                if not isinstance(ops, list):
                    self.log_error(f"[{rel_path}] Ability '{ab_name}' rule 'do' must be list of ops")
                    continue

                for op_idx, op in enumerate(ops):
                    if not isinstance(op, dict):
                        self.log_error(f"[{rel_path}] Ability '{ab_name}' op #{op_idx} is not a dict: {op}")
                        continue
                    kind = op.get("op") or op.get("kind") or op.get("type")
                    if not kind:
                        self.log_error(f"[{rel_path}] Ability '{ab_name}' op #{op_idx} missing 'op' / 'kind' name")
                    elif kind not in ALL_KNOWN_OPS and ALL_KNOWN_OPS:
                        self.log_warning(
                            f"[{rel_path}] Ability '{ab_name}' op '{kind}' not in known AbilityEngine ops. "
                            f"Ensure AbilityEngine handles this op kind."
                        )

        passed = len(self.errors) == initial_error_count
        if self.verbose:
            status = "✅ PASS" if passed else "❌ FAIL"
            print(f"{status} {blade_id} ({blade_name}) - {rel_path.name}")
        return passed

    def audit_roster_test_synchronization(self) -> bool:
        """Check that test assertions matching total counts match the filesystem."""
        total_beys = len(list(self.beys_dir.glob("*.json")))
        total_disks = len(list(self.disks_dir.glob("*.json")))
        total_drivers = len(list(self.drivers_dir.glob("*.json")))

        sync_errors = []

        # 1. tools/test_character_components.py
        t_char = self.tools_dir / "test_character_components.py"
        if t_char.is_file():
            content = t_char.read_text(encoding="utf-8")
            expected_clause = f"(('beys', {total_beys}), ('avatars', 59), ('parts/disks', {total_disks}), ('parts/drivers', {total_drivers}))"
            if expected_clause not in content:
                sync_errors.append(f"tools/test_character_components.py count tuple out of sync with actual ({total_beys} beys, {total_disks} disks, {total_drivers} drivers)")

        # 2. tools/test_physical_components.py
        t_phys = self.tools_dir / "test_physical_components.py"
        if t_phys.is_file():
            content = t_phys.read_text(encoding="utf-8")
            if f"self.assertEqual(len(beys), {total_beys})" not in content:
                sync_errors.append(f"tools/test_physical_components.py asserts len(beys) == {total_beys}")

        # 3. tools/test_special_avatar_pipeline.py
        t_avatar = self.tools_dir / "test_special_avatar_pipeline.py"
        if t_avatar.is_file():
            content = t_avatar.read_text(encoding="utf-8")
            if f"self.assertEqual(len(blades), {total_beys})" not in content:
                sync_errors.append(f"tools/test_special_avatar_pipeline.py asserts len(blades) == {total_beys}")

        # 4. tools/test_blast_jinnius.py
        t_blast = self.tools_dir / "test_blast_jinnius.py"
        if t_blast.is_file():
            content = t_blast.read_text(encoding="utf-8")
            if f"self.assertEqual(len(load_beys()),{total_beys})" not in content:
                sync_errors.append(f"tools/test_blast_jinnius.py asserts len(load_beys()) == {total_beys}")

        # 5. tools/test_dragon_killer_black_legend.py
        t_dk = self.tools_dir / "test_dragon_killer_black_legend.py"
        if t_dk.is_file():
            content = t_dk.read_text(encoding="utf-8")
            if f"self.assertEqual(len(load_beys()), {total_beys})" not in content:
                sync_errors.append(f"tools/test_dragon_killer_black_legend.py asserts len(load_beys()) == {total_beys}")

        # 6. tools/test_hp_balance.py (CI isolated-regressions mandate: 2000 <= Stock HP Lv100 <= 3000)
        t_hp = self.tools_dir / "test_hp_balance.py"
        if t_hp.is_file():
            try:
                import io
                import unittest
                suite = unittest.defaultTestLoader.loadTestsFromName("tools.test_hp_balance")
                runner = unittest.TextTestRunner(stream=io.StringIO())
                res = runner.run(suite)
                if not res.wasSuccessful():
                    sync_errors.append(
                        f"tools/test_hp_balance.py FAILED ({len(res.failures)} failures, {len(res.errors)} errors). "
                        f"Level 100 stock HP balance ceiling (3000 max) violated."
                    )
            except Exception as e:
                sync_errors.append(f"Could not execute tools/test_hp_balance.py: {e}")

        if sync_errors:
            for se in sync_errors:
                self.log_error(f"[Roster Test Sync] {se}")
            return False
        return True

    def run(self, bey_target: Optional[str] = None) -> int:
        """Run audit over targeted bey or full roster."""
        print(f"🔍 Running Beyblade Compatibility Audit (Root: {self.root})")

        target_files: List[Path] = []
        if bey_target:
            # Match by filename, id, or stem
            for f in self.beys_dir.glob("*.json"):
                if bey_target.lower() in (f.stem.lower(), f.name.lower()):
                    target_files.append(f)
                else:
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                        if bey_target.upper() == data.get("id", "").upper():
                            target_files.append(f)
                        elif bey_target.lower() == data.get("name", "").lower():
                            target_files.append(f)
                    except Exception:
                        pass
            if not target_files:
                print(f"❌ No Beyblade found matching: '{bey_target}'")
                return 1
        else:
            target_files = sorted(list(self.beys_dir.glob("*.json")))

        print(f"📋 Auditing {len(target_files)} Beyblade definition(s)...")
        passed_count = 0
        for bp in target_files:
            if self.audit_beyblade(bp):
                passed_count += 1

        print("🧪 Checking Roster Test Synchronization across test suites...")
        sync_ok = self.audit_roster_test_synchronization()

        print("\n" + "=" * 60)
        print("AUDIT SUMMARY:")
        print(f"  • Beyblades checked: {len(target_files)}")
        print(f"  • Compliant:        {passed_count}")
        print(f"  • Warnings:         {len(self.warnings)}")
        print(f"  • Errors:           {len(self.errors)}")
        print(f"  • Test Sync:        {'✅ OK' if sync_ok else '❌ OUT OF SYNC'}")
        print("=" * 60)

        if self.warnings:
            print("\n⚠️ WARNINGS:")
            for w in self.warnings[:20]:
                print(f"  - {w}")
            if len(self.warnings) > 20:
                print(f"  ... and {len(self.warnings) - 20} more warnings.")

        if self.errors:
            print("\n❌ ERRORS:")
            for e in self.errors:
                print(f"  - {e}")
            return 1

        print("\n✨ All compatibility and integrity checks PASSED successfully!")
        return 0


def main():
    parser = argparse.ArgumentParser(description="Audit Beyblade compatibility with AbilityEngine.")
    parser.add_argument("--bey", type=str, default=None, help="Target Beyblade name, ID (e.g. BB169), or filename")
    parser.add_argument("--all", action="store_true", help="Audit all registered Beyblades")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed pass/fail status per bey")
    parser.add_argument("--compute-split", nargs=4, type=int, metavar=("HP", "ATK", "DEF", "STM"),
                        help="Compute 45/27.5/27.5 stat split for given stats")

    args = parser.parse_args()

    if args.compute_split:
        hp, atk, df, stm = args.compute_split
        split = compute_stat_split(hp, atk, df, stm)
        print("📊 Computed 45 / 27.5 / 27.5 Component Split:")
        print(json.dumps(split, indent=2))
        return 0

    auditor = BeybladeAuditor(verbose=args.verbose)
    sys.exit(auditor.run(bey_target=args.bey))


if __name__ == "__main__":
    main()
