"""
cogs/log_formatter.py
---------------------
Battle log formatter: deduplicates, groups, and cleans up battle messages.

This module takes the raw log list from a battle round and:
  1. Deduplicates identical messages
  2. Groups repeated ability effects (e.g., "Ability Name x3: +21 HP")
  3. Consolidates stat changes (e.g., multiple Stability losses)
  4. Removes redundant status updates
  5. Adds consistent emoji and formatting

This ensures the battle log stays readable without spam while preserving
all mechanical information.
"""

import re
from typing import Any


class BattleLogFormatter:
    """Formats and deduplicates battle log messages."""

    def __init__(self):
        # Pattern templates for grouping similar messages
        self.ability_patterns = [
            (r"  ⛓️|chain", "chain"),           # Chain effects
            (r"  💥|**.*?\*\*.*?dmg", "damage"), # Damage announcements
            (r"  🔰|**Counter!**", "counter"),   # Counter hits
            (r"  🌊|shockwave", "shockwave"),    # Shockwave effects
            (r"  💀|KO", "ko"),                  # KO effects
            (r"  ✨|heal", "heal"),              # Heal effects
            (r"  🔥|burn|Burn", "burn"),         # Burn DoT
            (r"  ❄️|freeze|Freeze", "freeze"),  # Freeze DoT
            (r"  ☠️|poison|Poison", "poison"),  # Poison DoT
            (r"  ⚡|shock|Shock", "shock"),      # Shock DoT
            (r"  ☄️|curse|Curse", "curse"),      # Curse DoT
        ]

    def clean_logs(self, logs: list[str]) -> list[str]:
        """Main entry point: takes raw logs and returns cleaned/grouped logs.
        
        Steps:
          1. Deduplicate identical lines
          2. Group repeated ability messages
          3. Consolidate stat changes
          4. Group similar effects
          5. Return final clean list
        """
        if not logs:
            return []

        # Step 1: Collapse exact duplicates, but KEEP THE COUNT.
        #
        # This used to drop repeats outright. Repeats are not noise here — a
        # multi-hit Special calls the per-hit and defender triggers once per
        # hit, so an ability that fired 3 times produced 3 identical lines and
        # the log showed 1. The effect really was applying three times; only
        # the display said otherwise, which made working mechanics look broken.
        #
        # Lines that _group_ability_messages can fold into a total (those with
        # an explicit +N HP / dmg value) are passed through untouched so it can
        # still sum them. Everything else gets an "xN" marker.
        counts: dict[str, int] = {}
        order: list[str] = []
        summable: list[str] = []
        for log in logs:
            if re.search(r"([+-]?\d+)\s*(HP|dmg)", log) and re.search(r"\*\*([^*]+)\*\*", log):
                summable.append(log)          # let step 2 total these up
                continue
            if log not in counts:
                counts[log] = 0
                order.append(log)
            counts[log] += 1

        deduped = summable + [
            (line if counts[line] == 1 else f"{line}  ×{counts[line]}")
            for line in order
        ]

        # Step 2: Group ability messages by name
        grouped = self._group_ability_messages(deduped)

        # Step 3: Consolidate stat changes (stability, stamina, etc.)
        consolidated = self._consolidate_stat_changes(grouped)

        # Step 4: Group similar effect types
        final = self._group_similar_effects(consolidated)

        return final

    def _group_ability_messages(self, logs: list[str]) -> list[str]:
        """Group repeated identical ability messages into a summary.
        
        Example:
            Input:  ["  ⛓️ Requiem Spirit Chain — Chain heal +7 HP",
                     "  ⛓️ Requiem Spirit Chain — Chain heal +7 HP",
                     "  ⛓️ Requiem Spirit Chain — Chain heal +7 HP"]
            Output: ["⛓️ Requiem Spirit Chain",
                     "└ Chain Heal x3: +21 HP"]
        """
        result = []
        ability_groups: dict[str, list[str]] = {}

        for log in logs:
            # Extract ability name using regex
            match = re.search(r"\*\*([^*]+)\*\*", log)
            if match:
                ability_name = match.group(1)
                # Extract healing or damage value if present
                value_match = re.search(r"([+-]?\d+)\s*(HP|dmg)", log)
                if value_match:
                    value = int(value_match.group(1))
                    # Create a composite key that includes both ability name AND value
                    # This prevents "Ability +45 dmg", "Ability +50 dmg" from being incorrectly summed
                    base_log = log.split(" — ")[0] if " — " in log else log
                    effect_key = f"{ability_name}#{value}"
                    
                    if effect_key not in ability_groups:
                        ability_groups[effect_key] = []
                    ability_groups[effect_key].append((base_log, value, log))
                    continue

            # If no ability pattern matched, add as-is
            result.append(log)

        # Process grouped abilities
        for composite_key, group_list in ability_groups.items():
            # Unpack composite key to get ability name and value
            ability_name, value_str = composite_key.split('#')
            value = int(value_str)
            
            if len(group_list) == 1:
                # Only one occurrence, add as-is
                result.append(group_list[0][2])
            else:
                # Multiple occurrences with the SAME value, create summary
                base_log = group_list[0][0]
                total_value = sum(v for _, v, _ in group_list)
                count = len(group_list)
                
                # Determine effect type from the log
                if "+HP" in group_list[0][2] or "heal" in group_list[0][2].lower():
                    result.append(f"✨ {ability_name}")
                    result.append(f"└ Heal x{count}: {total_value:+d} HP")
                elif "dmg" in group_list[0][2].lower():
                    result.append(f"💥 {ability_name}")
                    result.append(f"└ Damage x{count}: {total_value:+d} dmg")
                else:
                    # Fallback: just show count
                    result.append(f"⚡ {ability_name} ×{count}")

        return result

    def _consolidate_stat_changes(self, logs: list[str]) -> list[str]:
        """Group multiple stat-change messages into a summary.
        
        Example:
            Input:  ["Stability -10 → 25", "Stability -10 → 15"]
            Output: ["💥 Stability Loss: -20 total", "Current Stability: 15"]
        """
        result = []
        stability_changes: list[int] = []
        stamina_changes: list[int] = []
        last_stability = None
        final_stamina = None

        for log in logs:
            # Match: "Stability -10 → 25"
            stab_match = re.match(r"Stability\s*([+-]\d+)\s*→\s*(\d+)", log)
            if stab_match:
                change = int(stab_match.group(1))
                final = int(stab_match.group(2))
                stability_changes.append(change)
                last_stability = final  # Track last observed final value
                continue

            # Match: "Stamina +1 → 8/15"
            sta_match = re.match(r"Stamina\s*([+-]\d+)", log)
            if sta_match:
                change = int(sta_match.group(1))
                stamina_changes.append(change)
                continue

            result.append(log)

        # Add consolidated stability changes
        if stability_changes:
            total = sum(stability_changes)
            if total != 0:
                result.append(f"💥 Stability Change: {total:+d} total")
            if last_stability is not None:
                result.append(f"   → Current: {last_stability}")

        # Add consolidated stamina changes (if any)
        if stamina_changes:
            total = sum(stamina_changes)
            if total != 0:
                result.append(f"⚡ Stamina: {total:+d}")

        return result

    def _group_similar_effects(self, logs: list[str]) -> list[str]:
        """Group messages by effect category for visual organization.
        
        Organizes effects into:
          - Ability activations
          - Damage & counters
          - Status effects (burns, freezes, etc.)
          - Stat changes (stability, stamina)
        """
        # For now, just return logs as-is. This is a placeholder
        # for more advanced grouping if needed in the future.
        # The deduplication and consolidation above handle most cases.
        return logs


def format_battle_logs(logs: list[str]) -> list[str]:
    """Convenience function: format a list of battle logs.
    
    Called from attack_manager.build_round_summary() to clean the extra_logs
    before they're assembled into the round summary embed.
    """
    formatter = BattleLogFormatter()
    return formatter.clean_logs(logs)
