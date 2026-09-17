"""Battle-local Purification Domain. Its lifecycle ticks once at round end.

The activation round is round one. Binary debuffs retain half duration (ceil);
quantitative debuffs retain half strength. Action costs exclude enemy-inflicted
stability damage. Domain stability is temporary capacity/current stability,
removed on expiry without restoring stability spent during the Domain.
"""
import math


def domains(session):
    status = getattr(session, "status", None)
    if status is None:
        return {}
    if not hasattr(status, "purification_domains"):
        status.purification_domains = {}
    return status.purification_domains


def active(session, key):
    return domains(session).get(key)


def enemy_domain(session, key):
    return next((d for d in domains(session).values() if d["enemy"] == key), None)


def stat_bonus(session, key, stat):
    if stat not in ("attack", "defense"):
        return 0
    base = session.blades.get(key, {}).get("stats", {}).get(stat, 0)
    pct = (0.20 if active(session, key) else 0) - (0.05 if enemy_domain(session, key) else 0)
    return round(base * pct)


def effective_stats(session, key):
    base = session.blades[key].get("stats", {})
    mult = getattr(session, "stat_mult", {}).get(key, 1.0)
    parts = getattr(session, "part_deltas", {}).get(key, {})
    avatar = getattr(session, "avatar_bonuses", {}).get(key)
    result = {}
    for stat, method in (("attack", "apply_attack_bonus"), ("defense", "apply_defence_bonus"), ("stamina", "apply_stamina_bonus")):
        value = (base.get(stat, 0) + session.status.get_buff_bonus(key, stat) + parts.get(stat, 0)) * mult
        if avatar and avatar.has_any_bonus:
            value = getattr(avatar, method)(value)
        result[stat] = max(0, int(value))
    return result


def open_domain(session, key, enemy, turns=4):
    # Recasting refreshes, never stacks temporary stability or conversion.
    close_domain(session, key)
    st = session.status
    while st.cleanse_one(key):
        pass
    st.active_buffs[key] = [b for b in st.active_buffs.get(key, [])
                            if not (b["amount"] < 0 and b.get("hostile", False))]
    defense = getattr(session, "defense_manager", None)
    if defense is not None:
        defense.grind_turns[key] = 0
    ability = session.ability
    getattr(ability, "ward_turns", {}).pop(key, None)
    sm = getattr(session, "stability_manager", None)
    bonus = round(sm.max[key] * 0.15) if sm else 0
    if sm:
        sm.max[key] += bonus
        sm.stability[key] += bonus
        sm.purification_session = session
    stamina = getattr(session, "stamina_manager", None)
    if stamina:
        stamina.purification_session = session
    domains(session)[key] = {"enemy": enemy, "turns": turns, "bonus": bonus,
                             "conversion": 0, "marks": 0, "judged": False, "seen": set()}
    return ["🌩️ **Purification Domain** — Purified State for 4 rounds; negative effects cleansed!"]


def close_domain(session, key):
    d = domains(session).pop(key, None)
    sm = getattr(session, "stability_manager", None)
    if d and sm:
        sm.max[key] -= d["bonus"]
        sm.stability[key] = max(0, min(sm.max[key], sm.stability[key] - d["bonus"]))


def tick(session):
    logs = []
    for key, d in list(domains(session).items()):
        d["turns"] -= 1
        d["seen"].clear()
        if d["turns"] <= 0:
            close_domain(session, key)
            logs.append("🌩️ **Purification Domain** expires — all Domain effects end.")
    return logs


def reduce_debuff(session, key, amount):
    d = active(session, key)
    if not d or not amount:
        return amount
    d["conversion"] = min(15, d["conversion"] + 5)
    return math.copysign(math.ceil(abs(amount) / 2), amount)


def heal_amount(session, key, amount):
    return math.floor(amount * 0.75) if enemy_domain(session, key) else amount


def action_cost(session, key, amount):
    mult = (0.75 if active(session, key) else 1) * (1.30 if enemy_domain(session, key) else 1)
    return math.ceil(amount * mult)


def amplify(session, key, damage, logs, first=True, last=True):
    d = active(session, key)
    if not d:
        return damage
    if first:
        d.pop("action_conversion", None)
    if damage > 0:
        if not d.get("action_conversion") and d["conversion"]:
            d["action_conversion"] = d["conversion"]
            d["conversion"] = 0
            logs.append(f"✨ **Purity Conversion** — +{d['action_conversion']}% damage!")
        damage = math.ceil(damage * (1 + d.get("action_conversion", 0) / 100))
    if last:
        d.pop("action_conversion", None)
    return damage


def mark(session, actor, token, logs):
    for owner, d in list(domains(session).items()):
        if d["enemy"] != actor or d["judged"] or token in d["seen"]:
            continue
        d["seen"].add(token)
        d["marks"] += 1
        logs.append(f"✨ **Purity Mark** — {d['marks']}/2!")
        if d["marks"] >= 2:
            d["judged"] = True
            damage = math.ceil(40 + 0.15 * effective_stats(session, owner)["attack"])
            ability = session.ability
            logs.extend(ability._check_revive(actor, session.blades[actor], damage,
                                              okey=owner, move="special", matchup="win"))
            session.hp[actor] = max(0, session.hp[actor] - damage)
            logs.append(f"⚡ **Purity Judgment** — {damage} true damage!")


def label(session, key):
    d = active(session, key)
    if d:
        return f"🌩️ Purified: {d['turns']}r · Conversion +{d['conversion']}% · Marks {d['marks']}/2"
    d = enemy_domain(session, key)
    return f"🌩️ Domain pressure: {d['turns']}r · Marks {d['marks']}/2" if d else ""
