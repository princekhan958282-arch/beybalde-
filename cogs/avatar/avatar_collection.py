"""Profile-backed ownership and spare copies, migrated from legacy ID lists."""
from . import avatar_progress as AP
from . import avatar_config as C


def migrate(profile, legacy=()):
    if 'avatar_inventory' not in profile:
        profile['avatar_inventory'] = list(dict.fromkeys(legacy))
        copies = profile.setdefault('avatar_copies', {})
        for aid in legacy:
            copies[aid] = max(int(copies.get(aid, 0)), list(legacy).count(aid) - 1)
    profile.setdefault('avatar_copies', {})
    for aid in profile['avatar_inventory']:
        entry = AP._ensure(profile, aid)
        entry.setdefault('stars', 1)
        entry.setdefault('stages', 0)
    return profile


def require_owned(profile, aid):
    if aid not in profile.get('avatar_inventory', []):
        raise AP.PurchaseError('You no longer own this avatar.')


def grant(profile, aid):
    owned = profile['avatar_inventory']
    if aid in owned:
        copies = profile['avatar_copies']
        copies[aid] = int(copies.get(aid, 0)) + 1
        return False
    owned.append(aid)
    AP._ensure(profile, aid).update(stars=1, stages=0)
    return True


def stars(profile, aid):
    return max(1, min(C.MAX_STARS, int(AP.card_entry(profile, aid).get('stars', 1))))


def stages(profile, aid):
    return max(0, min(C.STAGE_COUNT, int(AP.card_entry(profile, aid).get('stages', 0))))


def spare_copies(profile, aid):
    return max(0, int(profile.get('avatar_copies', {}).get(aid, 0)))


def consume(profile, aid, count):
    if spare_copies(profile, aid) < count:
        raise AP.PurchaseError(f'You need {count} spare copies; you own {spare_copies(profile, aid)}.')
    profile['avatar_copies'][aid] -= count


def feed(profile, aid, expected_stage):
    require_owned(profile, aid)
    if stars(profile, aid) != C.SAFE_STARS:
        raise AP.PurchaseError('Stages can only be filled at 5★.')
    now = stages(profile, aid)
    if now != expected_stage:
        raise AP.PurchaseError('Stage progress changed. Open the avatar again.')
    if now >= C.STAGE_COUNT:
        raise AP.PurchaseError('All five stages are already filled.')
    consume(profile, aid, C.STAGE_COPY_COST)
    AP._ensure(profile, aid)['stages'] = now + 1
    return {'stages': now + 1}


def upgrade_star(profile, aid, target, roll):
    require_owned(profile, aid)
    if target != stars(profile, aid) + 1 or target > C.MAX_STARS:
        raise AP.PurchaseError('Star rating changed or is already maxed. Open the avatar again.')
    if target == C.SAFE_STARS + 1 and stages(profile, aid) < C.STAGE_COUNT:
        raise AP.PurchaseError('Fill all five stages before trying 6★.')
    consume(profile, aid, C.STAR_COPY_COST[target])
    success = target <= C.SAFE_STARS or roll() < C.STAR_SUCCESS[target]
    if success:
        AP._ensure(profile, aid)['stars'] = target
    else:
        profile['avatar_inventory'].remove(aid)
        profile.get('avatar', {}).get('cards', {}).pop(aid, None)
        profile.get('avatar_skill', {}).pop(aid, None)
        if profile.get('equipped_avatar') == aid:
            profile['equipped_avatar'] = None
            profile.pop('avatar_skill_locked', None)
    return {'success': success, 'target': target, 'copies': C.STAR_COPY_COST[target]}


def stat_bonus(profile, card):
    steps = stars(profile, card['id']) - 1
    return {s: n * steps for s, n in C.STAR_STAT_GAIN.items()}
