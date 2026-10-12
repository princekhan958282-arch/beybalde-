"""Regression audit through real BattleSession rounds and owned stat pipelines."""
import copy
import math
import unittest
from dataclasses import replace
from unittest.mock import patch

from tools import sim_story as H
from cogs.avatar import NULL_BONUSES
from cogs.battle.battle import _apply_parts
from cogs.battle.purification import effective_stats
from cogs.battle.damage_rules import calc_damage
from utils import bey_levels as BL


async def session(name, enemy='Victory Valkyrie', *, level=100, reverse=False,
                  avatar=False, parts=False):
    names = [name, enemy]
    if reverse:
        names.reverse()
    blades = []
    for uid, bey in zip((101, 102), names):
        raw = copy.deepcopy(bey if isinstance(bey, dict) else H.get_beyblade(bey))
        bey = raw['name']
        profile = H.seed_profile(uid, blade=bey, inventory=[raw if raw.get('custom') else bey],
            bey_progress={bey: {'xp': BL.xp_for_level(level),
                               'ivs': dict.fromkeys(BL.STATS, 0)}},
            equipped_avatar='avatar_x002' if avatar else None,
            avatar_skill={'avatar_x002': 1}, avatar_energy=100)
        # Use the real component equipment resolver, including default parts.
        if parts:
            if not raw.get('custom'):
                from utils.bey_components import reconcile, equip
                profile['parts'] = ['Destroy Driver']
                reconcile(profile)
                equip(profile, 'Destroy Driver')
                H.STORE.put_one(uid, profile)
            raw = _apply_parts(raw, profile)
        blades.append(raw)
    s = await H.BattleSession.create(None, H.FakeChannel(),
        H.FakePlayer(101, 'A'), H.FakePlayer(102, 'B'), *blades,
        payout=False, avatars_enabled=avatar, bey_level=None if parts else level)
    s.type_gimmicks.rng = lambda: .99
    actor, target = ('102', '101') if reverse else ('101', '102')
    # Resource reserves let repeated sequences complete without ring-out.
    # Authored stats, levels, HP pools, skills and forms remain real.
    for key in (actor, target):
        s.stability_manager.stability[key] = s.stability_manager.max[key] = 1000
        s.stamina_manager.stamina[key] = 100
    return s, actor, target


async def round_trace(s, actor, target, move, reply, *, roll=.99):
    s.moves = {actor: move, target: reply}
    start = len(s.channel.sent)
    before = dict(s.hp)
    resources = {'stamina': dict(s.stamina_manager.stamina),
                 'stability': dict(s.stability_manager.stability),
                 'gauge': dict(s.stamina_manager.gauge)}
    stats = {k: effective_stats(s, k) for k in (actor, target)}
    resolve_pair = s.attack_manager.resolve_pair
    def record_pair(*args, **kwargs):
        stats[args[0]] = dict(args[6])
        stats[args[1]] = dict(args[7])
        return resolve_pair(*args, **kwargs)
    s.stamina_manager.gauge[actor] = 150 if move == 'special' else s.stamina_manager.gauge[actor]
    with patch('random.random', return_value=roll), patch.object(s.attack_manager, 'resolve_pair', side_effect=record_pair):
        await s._BattleSession__resolve_round_body()
    logs = []
    for message in s.channel.sent[start:]:
        embed = message.get('embed')
        if embed and embed.description:
            logs.append(embed.description)
    return {'round': s.round - 1, 'stats': stats, 'hp_before': before,
            'hp_after': dict(s.hp), 'damage': dict(s.attack_manager.committed_damage),
            'stacks': dict(s.ability.counters), 'cooldowns': dict(s.ability.cooldowns),
            'avatar_hit_counts': dict(getattr(s, '_avatar_hit_counts', {})),
            'resources_before': resources,
            'resources_after': {'stamina': dict(s.stamina_manager.stamina),
                                'stability': dict(s.stability_manager.stability),
                                'gauge': dict(s.stamina_manager.gauge)},
            'durations': {k: s.status.snapshot(k) for k in (actor, target)},
            'buffs': copy.deepcopy(s.status.active_buffs), 'logs': '\n'.join(logs)}


class RepeatedBattleAudit(unittest.IsolatedAsyncioTestCase):
    async def test_janus_amplified_partial_pierce_does_not_zero_defense(self):
        import itertools
        for reverse, level, avatar, parts in itertools.product((False, True), (1, 100), (False, True), (False, True)):
            with self.subTest(reverse=reverse, level=level, avatar=avatar, parts=parts):
                s, a, e = await session('Janus Bahamut', reverse=reverse,
                    level=level, avatar=avatar, parts=parts)
                self.assertEqual(effective_stats(s, a)['level'], level)
                for _ in range(4):
                    await round_trace(s, a, e, 'attack', 'defense')
                await round_trace(s, a, e, 'special', 'charge')
                trace = await round_trace(s, a, e, 'attack', 'charge')
                attacker, defender = trace['stats'][a], dict(trace['stats'][e])
                defender['defense'] = max(1, min(defender['defense'], attacker['attack']))
                raw, *_ = calc_damage('attack', attacker, defender, s.blades[a], 'charge')
                expected = math.floor(raw + math.ceil(raw * .30))
                self.assertEqual(trace['damage'][a], min(trace['hp_before'][e], expected), trace)
                self.assertEqual(trace['hp_before'][e] - trace['hp_after'][e], trace['damage'][a])

    async def test_one_special_is_one_master_win(self):
        for reverse in (False, True):
            s, a, e = await session('Master Diabolos', reverse=reverse, level=1)
            await round_trace(s, a, e, 'special', 'charge')
            self.assertEqual(s.ability.counters[a, 'master'], 1)
            self.assertEqual(s.ability.counters[a, 'master_def'], 1)

    async def test_passive_star_grows_once_per_round_during_special(self):
        s, a, e = await session('Aetherion Drakos org', level=1)
        await round_trace(s, a, e, 'special', 'charge')
        self.assertEqual(s.ability.counters[a, 'stars'], 1)

    async def test_dodged_cobalt_hit_does_not_heal(self):
        for reverse in (False, True):
            s, a, e = await session('Cobalt Dragoon', level=1, reverse=reverse)
            s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
            s.hp[a] -= 100
            trace = await round_trace(s, a, e, 'attack', 'charge', roll=0)
            self.assertEqual(trace['damage'][a], 0)
            self.assertEqual(trace['hp_after'][a], trace['hp_before'][a], trace)
            self.assertNotIn('Lifesteal', trace['logs'])

    async def test_blocked_or_dodged_blood_hits_do_not_bank_stacks(self):
        for reverse in (False, True):
            for level in (1, 100):
                for block in ('shield', 'dodge', 'kinetic'):
                    for move in ('attack', 'special'):
                        if block == 'kinetic' and move == 'special':
                            continue  # Kinetic mitigates Specials; it doesn't nullify them.
                        with self.subTest(reverse=reverse, level=level, block=block, move=move):
                            s, a, e = await session('Blood Dragon', 'Draciel G', level=level, reverse=reverse)
                            if block == 'shield':
                                s.status.add_shield(e, 100000)
                            elif block == 'dodge':
                                s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
                            else:
                                s.type_gimmicks.forced.add((e, 'kinetic_counter'))
                            for _ in range(4):
                                trace = await round_trace(s, a, e, move, 'defense' if block == 'kinetic' else 'charge', roll=0 if block == 'dodge' else .99)
                                self.assertEqual(trace['damage'][a], 0)
                                self.assertEqual(s.ability.counters.get((a, 'unstable'), 0), 0)
                            self.assertEqual(s.status.get_buff_bonus(a, 'attack'), 0)

    async def test_real_successful_blood_hits_keep_authored_stack_cap(self):
        for reverse in (False, True):
            s, a, e = await session('Blood Dragon', level=1, reverse=reverse)
            for _ in range(5):
                s.stamina_manager.stamina[a] = 20
                await round_trace(s, a, e, 'attack', 'defense')
            self.assertEqual(s.ability.counters[a, 'unstable'], 5)
            s.stamina_manager.stamina[a] = 20
            await round_trace(s, a, e, 'special', 'charge')
            self.assertEqual(s.ability.counters[a, 'unstable'], 7)
            for _ in range(3):
                s.stamina_manager.stamina[a] = 20
                await round_trace(s, a, e, 'attack', 'charge')
            self.assertEqual(s.ability.counters[a, 'unstable'], 10)
            self.assertEqual(s.status.get_buff_bonus(a, 'attack'), 80)

    async def test_cure_special_pierce_and_rule_cooldown_are_scoped(self):
        for name in ('Cure Black', 'Cure White'):
            for reverse in (False, True):
                s, a, e = await session(name, level=1, reverse=reverse)
                self.assertEqual(s.ability.pierce_pct(a, s.blades[a]), 0)
                s.hp[a] -= 100
                traces = [await round_trace(s, a, e, 'special', 'charge') for _ in range(5)]
                healed = [t['hp_after'][a] - t['hp_before'][a] for t in traces]
                recovery = 8 if name == 'Cure Black' else 12
                self.assertEqual(healed, [recovery, 0, 0, 0, recovery])
                self.assertEqual(s.ability.pierce_pct(a, s.blades[a]), 0)

    async def test_blocked_avatar_hit_does_not_advance_nth_strike(self):
        for reverse in (False, True):
            s, a, e = await session('Victory Valkyrie', 'Spriggan Requiem', level=1, reverse=reverse)
            s.avatar_bonuses[a] = replace(NULL_BONUSES, nth_hit_interval=2, nth_hit_attack_percent=.5)
            s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
            for _ in range(4):
                await round_trace(s, a, e, 'attack', 'charge', roll=0)
            self.assertEqual(s._avatar_hit_counts.get(a, 0), 0)
            s.avatar_bonuses[e] = NULL_BONUSES
            first = await round_trace(s, a, e, 'attack', 'charge')
            second = await round_trace(s, a, e, 'attack', 'charge')
            self.assertEqual(s._avatar_hit_counts[a], 2)
            self.assertNotIn('every 2th strike', first['logs'])
            self.assertIn('every 2th strike', second['logs'])

    async def test_silence_disables_janus_passive_pierce(self):
        for reverse in (False, True):
            s, a, e = await session('Janus Bahamut', level=100, reverse=reverse)
            s.status.silenced_turns[a] = 3
            trace = await round_trace(s, a, e, 'attack', 'defense')
            self.assertEqual(s.ability.pierce_pct(a, s.blades[a]), 0)
            self.assertNotIn('Defense Pierce', trace['logs'])

    async def test_custom_loss_growth_and_shatter_respect_cooldown_and_duration(self):
        from utils.custom_bey import build
        import itertools
        custom = build('Loss Audit', 'Attack', 100, 100, 100, 95,
            ['attack_growth', 'defense_shatter'], 'Strike', 120, 'none',
            'https://example.com/bey.png', ['attack_loss', 'attack_loss'])
        for level, reverse, avatar, parts in itertools.product((1, 100), (False, True), (False, True), (False, True)):
            with self.subTest(level=level, reverse=reverse, avatar=avatar, parts=parts):
                s, a, e = await session(custom, level=level, reverse=reverse, avatar=avatar, parts=parts)
                for _ in range(5):
                    s.stamina_manager.stamina[a] = 20
                    trace = await round_trace(s, a, e, 'attack', 'defense')
                    self.assertGreater(effective_stats(s, e)['defense'], 0)
                self.assertEqual(s.ability.counters[a, 'custom_attack_growth'], 2)
                self.assertEqual(s.status.get_buff_bonus(a, 'attack'), 6)  # Balanced preset: +3 per stack.
                # Advance through the next-round debuff lifetime.
                for _ in range(3):
                    await round_trace(s, a, e, 'charge', 'charge')
                self.assertEqual(s.status.get_buff_bonus(e, 'defense'), 0)

    async def test_inactive_adapters_preserve_critical_formula_precision(self):
        s, a, e = await session('Victory Valkyrie', 'Blood Dragon', level=100)
        s.blades[a]['abilities'] = []
        s.ability._compiled.clear()
        s.type_gimmicks.forced.add((a, 'critical_strike'))
        before = effective_stats(s, a), effective_stats(s, e)
        raw, *_ = calc_damage('attack', *before, s.blades[a], 'charge')
        trace = await round_trace(s, a, e, 'attack', 'charge')
        self.assertEqual(trace['damage'][a], math.floor(raw * 2))

    async def test_counter_dependent_hits_keep_third_hit_payoffs_and_ignore_dodges(self):
        for name, counter in [('Guilty Longinus', 'guilty_upper'), ('Azure Drakonyx', 'rampage_stack')]:
            for reverse in (False, True):
                s, a, e = await session(name, 'Noctilune', level=1, reverse=reverse)
                s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
                for _ in range(3):
                    await round_trace(s, a, e, 'attack', 'charge', roll=0)
                self.assertEqual(s.ability.counters.get((a, counter), 0), 0)
                s.avatar_bonuses[e] = NULL_BONUSES
                traces = []
                for _ in range(3):
                    s.stamina_manager.stamina[a] = 20
                    traces.append(await round_trace(s, a, e, 'attack', 'charge'))
                self.assertEqual(s.ability.counters[a, counter], 0)
                if name == 'Guilty Longinus':
                    self.assertIn('BURST! 40 TRUE damage', traces[-1]['logs'])
                else:
                    self.assertIn('25%', traces[-1]['logs'])

    async def test_radiant_dodge_does_not_spend_rush_cooldown(self):
        for reverse in (False, True):
            s, a, e = await session('Radiant Valkyrie', level=1, reverse=reverse)
            s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
            for _ in range(4):
                await round_trace(s, a, e, 'attack', 'charge', roll=0)
                self.assertEqual(s.ability.cooldowns.get((a, 'radiant_rush'), 0), 0)
            s.avatar_bonuses[e] = NULL_BONUSES
            trace = await round_trace(s, a, e, 'attack', 'charge')
            self.assertGreater(trace['damage'][a], 0)
            self.assertEqual(s.ability.cooldowns[a, 'radiant_rush'], 1)

    async def test_lifesteal_uses_overkill_bounded_hp_damage_in_both_orders(self):
        for reverse in (False, True):
            s, a, e = await session('Cobalt Dragoon', level=1, reverse=reverse)
            s.hp[a] -= 100
            s.hp[e] = 5
            trace = await round_trace(s, a, e, 'attack', 'charge')
            self.assertEqual(trace['damage'][a], 5)
            self.assertEqual(trace['hp_after'][a] - trace['hp_before'][a], 1)
            self.assertIn('healed **1** HP', trace['logs'])

    async def test_once_hit_rule_is_consumed_only_by_a_landed_move(self):
        # A supported DSL contract fixture, exercised through full rounds.
        for reverse in (False, True):
            s, a, e = await session('Victory Valkyrie', level=1, reverse=reverse)
            s.blades[a]['name'] = 'Once hit contract fixture'
            s.blades[a]['abilities'] = [{'name': 'Once hit fixture', 'rules': [{
                'when': 'on_attack_hit', 'once': 'battle', 'do': [
                    {'op': 'buff', 'stat': 'attack', 'amount': 5, 'turns': 99}]}]}]
            s.ability._compiled.clear()
            s.status.add_shield(e, 10000)
            for _ in range(4):
                await round_trace(s, a, e, 'attack', 'charge')
            self.assertFalse(s.ability.once_fired)
            self.assertEqual(s.status.get_buff_bonus(a, 'attack'), 0)
            s.status.shield_hp[e] = 0
            for _ in range(2):
                s.stamina_manager.stamina[a] = 20
                await round_trace(s, a, e, 'attack', 'charge')
            self.assertEqual(s.status.get_buff_bonus(a, 'attack'), 5)
            self.assertEqual(len(s.ability.once_fired), 1)

    async def test_twin_damage_reflect_does_not_fire_on_avatar_dodge(self):
        for reverse in (False, True):
            s, a, e = await session('Victory Valkyrie', 'Twin Nemesis', level=1, reverse=reverse)
            s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
            trace = await round_trace(s, a, e, 'attack', 'charge', roll=0)
            self.assertEqual(trace['damage'][a], 0)
            self.assertEqual(trace['hp_after'][a], trace['hp_before'][a])
            self.assertNotIn('reflected', trace['logs'])

    async def test_both_reflect_windows_work_in_an_attack_mirror(self):
        for reverse in (False, True):
            s, a, e = await session('Janus Bahamut', 'Noctilune', level=1, reverse=reverse)
            s.ability.reflect_windows[a] = [50, 3]
            s.ability.reflect_windows[e] = [50, 3]
            trace = await round_trace(s, a, e, 'attack', 'attack')
            for source, target in ((a, e), (e, a)):
                expected = trace['damage'][target] + math.ceil(trace['damage'][source] * .5)
                self.assertEqual(trace['hp_before'][source] - trace['hp_after'][source], expected)
            self.assertEqual(trace['logs'].count('actual HP damage'), 2)

    async def test_dodge_does_not_award_damage_taken_gauge(self):
        for reverse in (False, True):
            for move in ('attack', 'special'):
                s, a, e = await session('Blood Dragon', level=1, reverse=reverse)
                s.avatar_bonuses[e] = replace(NULL_BONUSES, dodge_chance=1)
                trace = await round_trace(s, a, e, move, 'charge', roll=0)
                self.assertEqual(trace['damage'][a], 0)
                self.assertEqual(s.stamina_manager.gauge[e], 50)  # Charge only.


if __name__ == '__main__':
    unittest.main()
