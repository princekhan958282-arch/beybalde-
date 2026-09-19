"""Opt-in battle-local effects. Percent fields use percentage points; cost modifiers also allow negatives.

No roster changes or persistent player data. The normal rules engine owns
trigger/condition/chance/once gates; this module owns bounded effect state.
"""
from copy import deepcopy
import math

OPS = frozenset({
    'damage_storage', 'release_storage', 'debuff_conversion', 'buff_suppression',
    'resource_conversion', 'delayed_echo', 'threshold_mark',
    'action_cost_modifier', 'cooldown_shift', 'shield_shatter', 'effect_transfer',
})
MOVES = frozenset({'attack', 'defense', 'stamina', 'charge', 'special'})
WINDOWS = OPS - {'release_storage', 'resource_conversion', 'threshold_mark',
                 'cooldown_shift', 'effect_transfer'}
# Threshold payloads cannot recursively create marks, transfer, or convert.
PAYLOAD_OPS = frozenset({'enemy_debuff', 'enemy_debuff_pct', 'buff', 'shield', 'log'})


def runtime(session):
    return getattr(getattr(session, 'ability', None), 'extended', None)


def number(value, low=0, high=100000):
    if isinstance(value, bool):
        raise ValueError('boolean is not a numeric effect value')
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'number must be finite and within {low}..{high}')
    return value


def validate(op):
    """Validate before any mutation; malformed new ops fail closed with a log."""
    kind = op['op']
    cfg = deepcopy(op)
    target = cfg.setdefault('target', 'self')
    if target not in ('self', 'enemy'):
        raise ValueError('target must be self or enemy')
    cfg.setdefault('name', kind)
    if not isinstance(cfg['name'], str) or not 1 <= len(cfg['name']) <= 80:
        raise ValueError('name must be a nonempty string up to 80 characters')
    for field in ('turns', 'delay', 'threshold', 'count'):
        if field in cfg:
            value = number(cfg[field], 1, 99)
            if value != int(value):
                raise ValueError(f'{field} must be an integer')
            cfg[field] = int(value)
    if kind in WINDOWS:
        cfg.setdefault('turns', 2)
    for field in ('pct', 'prevent_pct', 'gain_pct', 'max_pct'):
        if field in cfg and not (kind == 'action_cost_modifier' and field == 'pct'):
            cfg[field] = number(cfg[field], 0, 100)
    for field in ('cap', 'amount', 'rate'):
        if field in cfg:
            cfg[field] = number(cfg[field])
    if 'moves' in cfg:
        if not isinstance(cfg['moves'], list) or not cfg['moves'] or any(m not in MOVES for m in cfg['moves']):
            raise ValueError('moves must be a nonempty list of action names')
    if 'effects' in cfg:
        if not isinstance(cfg['effects'], list) or not cfg['effects'] or any(e not in ('attack', 'defense', 'stamina', 'burn', 'silence') for e in cfg['effects']):
            raise ValueError('unsupported effect filter')
    if kind in ('damage_storage', 'release_storage', 'debuff_conversion', 'delayed_echo', 'shield_shatter', 'effect_transfer') and target != 'self':
        raise ValueError('this effect requires target self')
    if kind == 'resource_conversion':
        if cfg.get('from') not in ('gauge', 'stamina', 'stability') or cfg.get('to') not in ('gauge', 'stamina', 'stability') or cfg['from'] == cfg['to']:
            raise ValueError('conversion needs two distinct supported resources')
        if cfg.get('amount', 0) <= 0 or cfg.get('rate', 0) <= 0:
            raise ValueError('amount and rate must be positive')
        if target != 'self':
            raise ValueError('resource conversion spends only your own resources')
    if kind == 'action_cost_modifier':
        if cfg.get('resource') not in ('stamina', 'stability'):
            raise ValueError('cost resource must be stamina or stability')
        cfg['pct'] = number(op.get('pct', 0), -90, 300)
    if kind in ('damage_storage', 'delayed_echo') and cfg.get('cap', 0) <= 0:
        raise ValueError('damage effects require an explicit positive cap')
    if kind == 'cooldown_shift':
        value = number(cfg.get('rounds', 0), -99, 99)
        if value != int(value):
            raise ValueError('cooldown rounds must be an integer')
        cfg['rounds'] = int(value)
    if kind == 'threshold_mark':
        payload = cfg.get('do', [])
        if not isinstance(payload, list) or len(payload) > 8 or any(not isinstance(p, dict) or p.get('op') not in PAYLOAD_OPS for p in payload):
            raise ValueError('mark payload must contain at most 8 supported nonrecursive ops')
        # Validate nested numeric inputs up front, rather than after consuming marks.
        for p in payload:
            if '_if' in p or 'if' in p:
                raise ValueError('put mark conditions on the outer rule')
            for field in ('value', 'amount', 'pct'):
                if field in p:
                    p[field] = number(p[field], -100000, 100000)
            if 'turns' in p:
                p['turns'] = int(number(p['turns'], 1, 99))
            if p.get('stat', 'attack') not in ('attack', 'defense', 'stamina'):
                raise ValueError('unsupported payload stat')
            if 'text' in p and not isinstance(p['text'], str):
                raise ValueError('log text must be a string')
    return cfg


class ExtendedEffects:
    def __init__(self, engine):
        self.engine = engine
        self.session = engine.session
        self.windows = {}  # (recipient, op, caster, name) -> owned independent window
        self.marks = {}    # (caster, recipient, name) -> count / lifetime
        self.echoes = []   # secondary damage never records new echoes/storage
        self.pending_conversion = {}
        for attr in ('stamina_manager', 'stability_manager'):
            manager = getattr(self.session, attr, None)
            if manager is not None:
                manager.effect_runtime = self

    def entries(self, key, kind):
        return [v for (k, op, _, _), v in self.windows.items() if k == key and op == kind]

    def execute(self, op, label, key, other, move, dealt, taken, logs, matchup):
        try:
            cfg = validate(op)
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            logs.append(f'⚠️ {label}: invalid {op.get("op")} skipped ({exc}).')
            return dealt, taken
        kind = cfg['op']
        target = other if cfg['target'] == 'enemy' else key
        identity = (target, kind, key, cfg['name'])
        hostile = target != key
        if hostile and self.engine._debuff_blocked(target, label, logs):
            return dealt, taken
        if kind in WINDOWS:
            if identity not in self.windows and len(self.windows) >= 128:
                logs.append(f'⚠️ {label}: active effect limit reached.')
                return dealt, taken
            # Refresh replaces config but cannot refill a spent bank or duplicate it.
            previous = self.windows.get(identity, {})
            cfg.update(owner=key, label=label, bank=previous.get('bank', 0))
            cfg['bank'] = min(cfg['bank'], cfg.get('cap', cfg.get('max_pct', 15)))
            self.windows[identity] = cfg
            logs.append(f'✨ **{label}** — {kind.replace("_", " ")} on {target} for {cfg["turns"]} rounds.')
        elif kind == 'release_storage':
            entry = self.windows.get((key, 'damage_storage', key, cfg['name']))
            if entry and entry['bank'] > 0:
                amount = int(entry['bank'])
                entry['bank'] = 0  # consume before damage to prevent feedback
                self.secondary_damage(key, other, amount, label, logs)
        elif kind == 'resource_conversion':
            self.convert(key, cfg, label, logs)
        elif kind == 'cooldown_shift':
            cd = (target, cfg['name'])
            before = self.engine.cooldowns.get(cd, 0)
            if before > 0:  # cannot invent a cooldown for an unknown/ready ability
                after = max(0, min(99, before + cfg['rounds']))
                self.engine.cooldowns[cd] = after
                logs.append(f'⏳ **{label}** — {cfg["name"]}: {before} → {after} rounds.')
        elif kind == 'threshold_mark':
            mark_id = (key, target, cfg['name'])
            if mark_id not in self.marks and len(self.marks) >= 128:
                return dealt, taken
            threshold = cfg.get('threshold', 3)
            count = min(threshold, self.marks.get(mark_id, {}).get('count', 0) + cfg.get('count', 1))
            self.marks[mark_id] = {'count': count, 'turns': cfg.get('turns', 3)}
            logs.append(f'🎯 **{label}** — {cfg["name"]}: {count}/{threshold}.')
            if count >= threshold:
                del self.marks[mark_id]
                dealt, taken = self.engine._run_ops({'do': cfg.get('do', [])}, label, key, other, move, dealt, taken, logs, matchup)
        elif kind == 'effect_transfer':
            self.transfer(key, other, cfg, label, logs)
        return dealt, taken

    def convert(self, key, cfg, label, logs):
        sm = getattr(self.session, 'stamina_manager', None)
        stab = getattr(self.session, 'stability_manager', None)
        if sm is None or stab is None:
            return
        from cogs.battle.constants import SPECIAL_GAUGE_MAX
        pools = {'gauge': (sm.gauge, SPECIAL_GAUGE_MAX),
                 'stamina': (sm.stamina, sm.cap_for(key)),
                 'stability': (stab.stability, stab.max.get(key, 0))}
        source, _ = pools[cfg['from']]
        dest, cap = pools[cfg['to']]
        cost = cfg['amount']
        gain = min(cap - dest.get(key, 0), cost * cfg['rate'])
        if cfg['to'] != 'stamina':
            gain = math.floor(gain)
        if source.get(key, 0) < cost or gain <= 0:
            return
        # Never spend fractional gauge/stability or end a battle via conversion.
        if cfg['from'] != 'stamina' and cost != int(cost):
            return
        if cfg['from'] in ('stamina', 'stability') and source[key] - cost <= 0:
            return
        source[key] -= cost if cfg['from'] == 'stamina' else int(cost)
        dest[key] = min(cap, dest.get(key, 0) + gain)
        logs.append(f'🔄 **{label}** — {cost:g} {cfg["from"]} → {gain:g} {cfg["to"]}.')

    def reduce_debuff(self, key, amount, effect):
        """Magnitude for numeric effects; duration for silence. No double reduction."""
        if not amount:
            return amount
        candidates = [c for c in self.entries(key, 'debuff_conversion')
                      if effect in c.get('effects', [effect])]
        if not candidates:
            return amount
        cfg = max(candidates, key=lambda c: c.get('prevent_pct', 50))
        remaining = math.ceil(abs(amount) * (1 - cfg.get('prevent_pct', 50) / 100))
        if remaining < abs(amount):
            cfg['bank'] = min(cfg.get('max_pct', 15), cfg['bank'] + cfg.get('gain_pct', 5))
        return remaining if amount > 0 else -remaining

    def buff_amount(self, key, stat, amount):
        if amount <= 0:
            return amount
        pct = max((c.get('pct', 0) for c in self.entries(key, 'buff_suppression')
                   if stat in c.get('effects', [stat])), default=0)
        return amount if not pct else amount * (1 - pct / 100)

    def cost(self, key, move, resource, amount):
        if amount <= 0:
            return amount
        pct = sum(c.get('pct', 0) for c in self.entries(key, 'action_cost_modifier')
                  if c['resource'] == resource and move in c.get('moves', MOVES))
        return amount if not pct else max(0.01, round(amount * (1 + max(-90, min(300, pct)) / 100), 2))

    def shield_multiplier(self, key):
        return 1 + max((c.get('pct', 100) for c in self.entries(key, 'shield_shatter')), default=0) / 100

    def before_hit(self, key, damage, first=True):
        if first:
            self.pending_conversion.pop(key, None)
        if damage <= 0 or self.session.status.is_silenced(key):
            return damage
        entries = self.entries(key, 'debuff_conversion')
        pending = self.pending_conversion.setdefault(key, {})
        for c in entries:
            pending[(c['owner'], c['name'])] = c['bank']
        pct = min(100, sum(c['bank'] for c in entries))
        return damage + math.ceil(damage * pct / 100)

    def committed(self, attacker, defender, move, actual, logs):
        """Called once for a whole action, AFTER avatar mitigation and HP clamp."""
        pending = self.pending_conversion.pop(attacker, {})
        if actual <= 0:
            return
        for c in self.entries(attacker, 'debuff_conversion'):
            spent = min(c['bank'], pending.get((c['owner'], c['name']), 0))
            if spent:
                logs.append(f'✨ **{c["label"]}** — conversion spent ({spent:g}%).')
                c['bank'] -= spent
        for c in self.entries(defender, 'damage_storage'):
            c['bank'] = min(c['cap'], c['bank'] + actual * c.get('pct', 20) / 100)
            logs.append(f'🛡️ **{c["label"]}** — stored {c["bank"]:g}/{c["cap"]:g}.')
        for c in self.entries(attacker, 'delayed_echo'):
            if move not in c.get('moves', ['special']) or len(self.echoes) >= 64:
                continue
            amount = min(c['cap'], math.floor(actual * c.get('pct', 15) / 100))
            if amount > 0:
                self.echoes.append({'owner': attacker, 'target': defender,
                                    'amount': amount, 'turns': c.get('delay', 1) + 1,
                                    'label': c['label']})
                logs.append(f'🔮 **{c["label"]}** — echo stores {amount:g} damage.')

    def secondary_damage(self, key, other, amount, label, logs):
        """Nonrecursive secondary hit; normal protection, avatar guard and revival."""
        s = self.session
        if s.hp.get(key, 0) <= 0 or s.hp.get(other, 0) <= 0:
            return
        st = s.status
        if st.is_invulnerable(other):
            logs.append(f'🛡️ **{label}** — blocked by invulnerability.')
            return
        evaded, lines = self.engine.damage_filter._step3b_evasion(key, other, s.blades[key], s.blades[other])
        logs.extend(lines)
        if evaded:
            return
        damage = max(0, int(amount))
        damage -= st.absorb_shield(other, damage)
        window = self.engine.resist_windows.get(other)
        if window:
            damage = max(0, damage - math.ceil(damage * window[0] / 100))
        from cogs.battle import avatar_combat as av
        damage, counter, lines = av.absorb_incoming(s, other, key, damage)
        logs.extend(lines)
        logs.extend(self.engine._check_revive(other, s.blades[other], damage, okey=key))
        damage, lines = av.guard_lethal(s, other, damage)
        logs.extend(lines)
        actual = min(s.hp[other], max(0, damage))
        s.hp[other] = max(0, s.hp[other] - actual)
        s.hp[key] = max(0, s.hp[key] - counter)
        logs.append(f'🔮 **{label}** — {actual} secondary damage.')

    def transfer(self, key, other, cfg, label, logs):
        # Bounded, explicit subset. Permanent tradeoffs, domains, marks and
        # resource penalties are deliberately not transferable.
        if self.engine._debuff_blocked(other, label, logs):
            return
        from .ability_engine import _avatar_resist
        blocked, lines = _avatar_resist(self.session, other, label)
        logs.extend(lines)
        if blocked:
            return
        st = self.session.status
        allowed = cfg.get('effects', ['attack', 'defense', 'stamina', 'burn', 'silence'])
        count = cfg.get('count', 1)
        for buff in list(st.active_buffs.get(key, [])):
            if count <= 0:
                break
            if buff['amount'] < 0 and buff.get('hostile', True) and 0 < buff['rounds_left'] < 99 and buff['stat'] in allowed:
                st.add_buff(other, buff['stat'], buff['amount'], buff['rounds_left'], source='effect_transfer')
                received = st.active_buffs[other][-1]
                if received['amount'] == 0:
                    st.active_buffs[other].remove(received)
                    continue
                st.active_buffs[key].remove(buff)
                logs.append(f'🔁 **{label}** — transferred {buff["stat"]} debuff.')
                count -= 1
        if count and 'silence' in allowed and st.silenced_turns.get(key, 0) > 0:
            before = st.silenced_turns.get(other, 0)
            st.silence(other, st.silenced_turns[key])
            if st.silenced_turns.get(other, 0) == before:
                return
            st.silenced_turns[key] = 0
            logs.append(f'🔁 **{label}** — transferred silence.')
            count -= 1
        if count and 'burn' in allowed and st.burn_duration.get(key, 0) > 0 and st.burn_stacks.get(key, 0) > 0:
            # A burn already on the receiver cannot be overwritten or weakened.
            if st.burn_stacks.get(other, 0) > 0 and st.burn_duration.get(other, 0) > 0:
                return
            logs.extend(st.apply_burn(other, {'name': label, 'burn_damage_per_turn': st.burn_dmg[key] * st.burn_stacks[key], 'burn_duration': st.burn_duration[key]}))
            if st.burn_dmg.get(other, 0) <= 0:
                st.burn_stacks[other] = st.burn_dmg[other] = st.burn_duration[other] = 0
                return
            st.burn_stacks[key] = st.burn_dmg[key] = st.burn_duration[key] = 0

    def tick(self):
        logs = []
        for identity, cfg in list(self.windows.items()):
            cfg['turns'] -= 1
            if cfg['turns'] <= 0:
                del self.windows[identity]
                logs.append(f'⌛ **{cfg["label"]}** — {cfg["op"].replace("_", " ")} expires.')
        for identity, cfg in list(self.marks.items()):
            cfg['turns'] -= 1
            if cfg['turns'] <= 0:
                del self.marks[identity]
        pending, self.echoes = self.echoes, []
        for echo in pending:
            echo['turns'] -= 1
            if echo['turns'] > 0:
                self.echoes.append(echo)
            else:
                self.secondary_damage(echo['owner'], echo['target'], echo['amount'], echo['label'], logs)
        return logs

    def snapshot(self, key):
        return deepcopy({'windows': [v for (k, _, _, _), v in self.windows.items() if k == key],
                         'marks': [dict(v, owner=k, name=n) for (k, t, n), v in self.marks.items() if t == key],
                         'echoes': [e for e in self.echoes if e['owner'] == key or e['target'] == key]})
