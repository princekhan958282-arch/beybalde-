"""Profile-backed cards and V4 same-category feeding, with lazy migration."""
from uuid import uuid4
from . import avatar_progress as AP
from . import avatar_config as C


def category(card):
    # Current roster uses rarity as its banner/category field.
    return str(card.get('category') or card.get('rarity') or 'Common')


def migrate(profile, legacy=()):
    if 'avatar_inventory' not in profile:
        ids = list(legacy)
        # Preserve equipped cards from pre-inventory profiles.
        equipped = profile.get('equipped_avatar')
        if equipped and equipped not in ids:
            ids.append(equipped)
        profile['avatar_inventory'] = list(dict.fromkeys(ids))
        copies = profile.setdefault('avatar_copies', {})
        for aid in ids:
            copies[aid] = max(int(copies.get(aid, 0)), ids.count(aid) - 1)
    profile.setdefault('avatar_copies', {})
    for aid in profile['avatar_inventory']:
        entry = AP._ensure(profile, aid)
        entry.setdefault('stars', 1)
        entry.setdefault('generation', uuid4().hex)
        # V3 stages represented already-consumed individual copies. Credit
        # them once, without consuming again or changing stored card levels.
        entry.setdefault('feeding', max(0, int(entry.get('stages', 0))))
    profile.setdefault('avatar_rewards', {})
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
    # A reacquired card is a new incarnation; old confirmations cannot touch it.
    profile.setdefault('avatar', {}).setdefault('cards', {}).pop(aid, None)
    AP._ensure(profile, aid).update(stars=1, feeding=0, generation=uuid4().hex)
    return True


def stars(profile, aid):
    return max(1, min(C.MAX_STARS, int(AP.card_entry(profile, aid).get('stars', 1))))


def stages(profile, aid):
    """Compatibility name for the persisted feeding-card count."""
    entry = AP.card_entry(profile, aid)
    return max(0, int(entry.get('feeding', entry.get('stages', 0))))


def spare_copies(profile, aid):
    return max(0, int(profile.get('avatar_copies', {}).get(aid, 0)))


def consume(profile, aid, count):
    if count <= 0 or spare_copies(profile, aid) < count:
        raise AP.PurchaseError(f'You need {count} spare copies; you own {spare_copies(profile, aid)}.')
    profile['avatar_copies'][aid] -= count


def eligible_materials(profile, aid, catalog):
    """Spare cards only: primary, equipped and upgraded cards cannot be spent."""
    target = catalog.get(aid)
    if not target:
        raise AP.PurchaseError('Avatar definition unavailable.')
    return {key: spare_copies(profile, key) for key, card in catalog.items()
            if category(card).casefold() == category(target).casefold()
            and spare_copies(profile, key) > 0}


def feed(profile, aid, expected_stage, materials, catalog, *, expected_star=None,
         generation=None):
    require_owned(profile, aid)
    entry = AP._ensure(profile, aid)
    star = stars(profile, aid)
    if (generation is not None and entry.get('generation') != generation or
            expected_star is not None and star != expected_star or
            stages(profile, aid) != expected_stage):
        raise AP.PurchaseError('Avatar or feeding progress changed. Open it again.')
    if star >= C.MAX_STARS:
        raise AP.PurchaseError('This avatar is already at 15★.')
    if star + 1 not in C.STAR_SUCCESS:
        raise AP.PurchaseError('Next star success rate is pending approval; feeding is locked.')
    if not materials or any(not isinstance(n, int) or isinstance(n, bool) or n <= 0
                            for n in materials.values()):
        raise AP.PurchaseError('Choose a positive number of feeding copies.')
    eligible = eligible_materials(profile, aid, catalog)
    for key, count in materials.items():
        if key not in eligible or eligible[key] < count:
            raise AP.PurchaseError('Materials must be available spare copies in the same category.')
    count = sum(materials.values())
    required = C.STAR_COPY_COST[star + 1]
    if expected_stage + count > required:
        raise AP.PurchaseError(f'Only {required - expected_stage} more cards are needed.')
    for key, n in materials.items():
        consume(profile, key, n)
    entry['feeding'] = expected_stage + count
    return {'stages': entry['feeding'], 'consumed': count}


def upgrade_star(profile, aid, target, roll, *, generation=None):
    require_owned(profile, aid)
    entry = AP._ensure(profile, aid)
    if generation is not None and entry.get('generation') != generation:
        raise AP.PurchaseError('This is a different copy. Open the avatar again.')
    if target != stars(profile, aid) + 1 or target > C.MAX_STARS:
        raise AP.PurchaseError('Star rating changed or is already maxed. Open the avatar again.')
    if target not in C.STAR_SUCCESS:
        raise AP.PurchaseError('Success rate pending approval. This upgrade is locked.')
    required = C.STAR_COPY_COST[target]
    if stages(profile, aid) < required:
        raise AP.PurchaseError(f'Feed {required} same-category cards before upgrading.')
    success = target <= C.SAFE_STARS or roll() < C.STAR_SUCCESS[target]
    if success:
        entry['stars'] = target
        entry['feeding'] = 0
        entry['stages'] = 0
    else:
        profile['avatar_inventory'].remove(aid)
        profile.get('avatar', {}).get('cards', {}).pop(aid, None)
        profile.get('avatar_skill', {}).pop(aid, None)
        if profile.get('equipped_avatar') == aid:
            profile['equipped_avatar'] = None
            profile.pop('avatar_skill_locked', None)
    return {'success': success, 'target': target, 'copies': required}


def stat_bonus(profile, card):
    total = C.star_stat_total(stars(profile, card['id']))
    return {s: total for s in ('attack', 'defense', 'stamina')}
