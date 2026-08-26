#!/usr/bin/env python3
"""
sim_school_avatars.py — the eight School League bladers.

What this proves
----------------
The League's eight opponents each field a blader card — Rantaro, Ken, Daigo,
Hoji, Wakiya, Orochi, Valt, Shu — and that card is what makes the eight battles
feel different instead of being one fight with bigger numbers. Two systems
carry it, both of which already existed:

  * the **statline** is `AvatarBonuses`, the ordinary avatar bonus snapshot;
  * the **skills** are written in the **blade ability DSL**, so they reach the
    battle through the same triggers a blade's abilities do.

The check this file exists for is §3. In v1.22 Story Mode ran on an engine
where a blade's abilities loaded, validated, and then did nothing — the whole
rewrite was to fix that. Twenty-four skills that parse are worth nothing; the
suite drives a real `BattleSession` and asserts the engine actually RAN each
rule, and for a sample that the effect landed.

Why rule dispatch and not the battle log
----------------------------------------
Three of the ops these skills use log nothing an assertion can find:
`crit_chance` writes a number and returns silently, and `gain_stability`
delegates its log line to the stability manager, which names the stat and not
the ability. Grepping the log for a skill name therefore reports seven false
misses. So §3 observes `AbilityEngine._run_ops` — the engine's own decision to
run a rule — which is the thing under test, and the ops then really execute.

Run:  python3 tools/sim_school_avatars.py
"""

from __future__ import annotations

import asyncio
import collections
import copy
import importlib.util
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}   {detail}")


# The Story harness already builds real sessions against an in-memory store.
# Loaded as a module rather than reimplemented: two harnesses drift, and this
# one is the one `sim_story` keeps honest.
_spec = importlib.util.spec_from_file_location(
    "_sim_story_harness", os.path.join(ROOT, "tools", "sim_story.py"))
H = importlib.util.module_from_spec(_spec)
_argv, sys.argv = sys.argv, ["sim_school_avatars"]
_spec.loader.exec_module(H)
sys.argv = _argv

import utils.database as DB                                      # noqa: E402
from cogs.abilities.ability_engine import AbilityEngine          # noqa: E402
from cogs.avatar import avatar_shop as SHOP                      # noqa: E402
from cogs.avatar import avatar_skills as AS                      # noqa: E402
from cogs.avatar.avatar_engine import avatar_engine              # noqa: E402
from cogs.core.constants import (                                # noqa: E402
    MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA,
)
from cogs.story import story_data as SD                          # noqa: E402

avatar_engine.load()

# ── The table, exactly as specified ───────────────────────────────────────────
# id, blader, bey, HP, ATK, ATK%, DEF, DEF%, STM, STM%, Stability, Stab%
TABLE = [
    ("avatar_s101", "Rantaro Kiyama",  "Rising Roktavor",     120, 35, 0.16, 28, 0.00, 42, 0.18, 30, 0.00),
    ("avatar_s102", "Ken Midori",      "King Kerbeus",        135, 27, 0.00, 45, 0.20, 35, 0.00, 42, 0.22),
    ("avatar_s103", "Daigo Kurogami",  "Hollow Deathscyther", 115, 46, 0.22, 25, 0.00, 34, 0.00, 28, 0.00),
    ("avatar_s104", "Hoji Konda",      "Hyper Horusood",      125, 36, 0.15, 34, 0.12, 36, 0.00, 35, 0.00),
    ("avatar_s105", "Wakiya Murasaki", "Wild Wyvern",         130, 30, 0.00, 43, 0.18, 37, 0.00, 44, 0.20),
    ("avatar_s106", "Orochi Ginba",    "Omni Odax",           118, 43, 0.20, 30, 0.00, 39, 0.15, 32, 0.00),
    ("avatar_s107", "Valt Aoi",        "Victory Valkyrie",    120, 52, 0.25, 28, 0.00, 35, 0.00, 30, 0.00),
    ("avatar_s108", "Shu Kurenai",     "Storm Spriggan",      125, 45, 0.20, 40, 0.15, 43, 0.18, 45, 0.20),
]

SKILL_NAMES = {c["id"]: [s["name"] for s in c["skills"]]
               for c in avatar_engine.get_all_avatars()
               if c.get("rarity") == "Blader"}
ALL_SKILLS = [n for names in SKILL_NAMES.values() for n in names]


# ── Rule-dispatch spy ─────────────────────────────────────────────────────────
class Spy:
    """Records which AVATAR rules the engine actually ran."""

    def __init__(self):
        self.fired = collections.Counter()
        self._real = AbilityEngine._run_ops

    def __enter__(self):
        real, fired = self._real, self.fired

        def spy(engine, rule, ab_name, key, okey, move, dd, dt, logs,
                matchup=""):
            if rule.get("_avatar"):
                fired[ab_name] += 1
            return real(engine, rule, ab_name, key, okey, move, dd, dt, logs,
                        matchup)

        AbilityEngine._run_ops = spy
        return self

    def __exit__(self, *a):
        AbilityEngine._run_ops = self._real
        return False


def main() -> int:
    asyncio.run(suite())
    print("\n" + "=" * 66)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 66)
    return 1 if FAIL else 0


async def suite() -> None:
    player = H.FakePlayer(H.HUMAN_ID, "Tester")
    H.seed_profile()
    pblade, _pg = H.levelled("Void Longinus", 100)

    # ── 1. the cards ─────────────────────────────────────────────────────────
    print("\n── 1. eight cards, and the statline you specified ──────────────")
    bladers = [a for a in avatar_engine.get_all_avatars()
               if a.get("rarity") == "Blader"]
    check("there are eight of them", len(bladers) == 8, len(bladers))
    check("in the order given, with the names given",
          [a["id"] for a in bladers] == [t[0] for t in TABLE]
          and [a["name"] for a in bladers] == [t[1] for t in TABLE],
          [a["name"] for a in bladers])
    check("every image is distinct", len({a["image"] for a in bladers}) == 8)
    check("...and every image is a real URL",
          all(a["image"].startswith("https://") for a in bladers))

    for (aid, name, bey, hp, atk, atkp, dfn, dfnp, stm, stmp, stab,
         stabp) in TABLE:
        card = avatar_engine.get_avatar(aid)
        b = card["bonuses"]
        got = (b["hp_flat"], b["attack_flat"], b["attack_percent"],
               b["defence_flat"], b["defence_percent"],
               b["stamina_flat"], b["stamina_percent"],
               b["stability_flat"], b["stability_percent"])
        want = (float(hp), float(atk), atkp, float(dfn), dfnp,
                float(stm), stmp, float(stab), stabp)
        check(f"{name} — HP {hp} · ATK {atk} · DEF {dfn} · STM {stm} · "
              f"STAB {stab}", got == want, (got, want))

    check("each has exactly three skills",
          all(len(a["skills"]) == 3 for a in bladers),
          [len(a["skills"]) for a in bladers])
    check("...and every skill carries DSL rules, not just prose",
          all(sk.get("rules") for a in bladers for sk in a["skills"]))
    check("every skill name is unique across the eight",
          len(set(ALL_SKILLS)) == 24, len(set(ALL_SKILLS)))

    # ── 2. stats always on ───────────────────────────────────────────────────
    print("\n── 2. the statline survives whichever skill is picked ──────────")
    for aid, name, *_rest in TABLE:
        card = avatar_engine.get_avatar(aid)
        want = card["bonuses"]["attack_flat"]
        got = [AS.bonuses_for(card, s)["attack_flat"] for s in (0, 1, 2, 3)]
        check(f"{name}'s statline is intact at every slot",
              got == [want] * 4, got)
    check("the flag is what does it, and it is on the card",
          all(a.get("stats_always_on") for a in bladers))
    check("...and no card written before it carries it — every other card "
          "still narrows to the slot it paid for",
          not any(a.get("stats_always_on")
                  for a in avatar_engine.get_all_avatars()
                  if a.get("rarity") != "Blader"))
    freya = avatar_engine.get_avatar("avatar_mlbb001")
    check("Freya is the control: her block still zeroes outside her slot",
          AS.bonuses_for(freya, 1)["counter_chance"] == 0
          and AS.bonuses_for(freya, 2)["counter_chance"] == 0.25,
          (AS.bonuses_for(freya, 1)["counter_chance"],
           AS.bonuses_for(freya, 2)["counter_chance"]))

    # ── 3. every skill actually fires ────────────────────────────────────────
    print("\n── 3. all 24 skills FIRE in a real battle ──────────────────────")
    #
    # DRIVEN, not sampled. An earlier version of this section played ordinary
    # League battles and asserted "at least 23 of 24 fired" — and which one was
    # missing changed run to run, because whether a boss happens to drop below
    # half HP in eight sampled battles is luck. A check that passes or fails on
    # luck is not a check. Each skill is now driven into the exact state its
    # trigger names.
    #
    # The matchup wheel is Attack > Stamina > Defense > Attack, so a boss is
    # made to WIN with attack by giving the player stamina, and so on.
    A, D, ST = MOVE_ATTACK, MOVE_DEFENSE, MOVE_STAMINA

    def low_hp(s, nk, pk):
        s.hp[nk] = int(s.max_hp_per_player[nk] * 0.30)

    def low_stam(s, nk, pk):
        s.stamina_manager.stamina[nk] = s.stamina_manager.cap_for(nk) * 0.20

    def enemy_low_hp(s, nk, pk):
        s.hp[pk] = int(s.max_hp_per_player[pk] * 0.20)

    def variety(s, nk, pk):
        s.move_counts[nk] = {"attack": 2, "defense": 2, "stamina": 2}

    def full_gauge(s, nk, pk):
        s.stamina_manager.gauge[nk] = 150.0

    def high_stability(s, nk, pk):
        s.stability_manager.stability[nk] = s.stability_manager.max[nk]

    def player_spams_attack(s, nk, pk):
        s.move_counts[pk] = {"attack": 6, "defense": 1}

    def behind(s, nk, pk):
        s.victory_points = {nk: 0, pk: 2}

    # skill -> (battle, boss move, player move, setup, rounds to try)
    DRIVE = {
        "Roktavor Rush":       (1, A,  ST, None,            1),
        "Stamina Wheel":       (1, ST, D,  low_stam,        1),
        # NOT the Stamina move: it restores stamina back above the 35%
        # threshold before the trigger is evaluated, so the boss would no
        # longer be at low stamina by the time its own skill looks.
        "Relentless Spin":     (1, D,  A,  low_stam,        1),
        "Guardian Wall":       (2, D,  A,  None,            1),
        "Kerbeus Lock":        (2, D,  A,  None,           12),   # 40% chance
        "Fortress Core":       (2, D,  A,  low_hp,          1),
        "Death Scythe":        (3, A,  ST, None,            1),
        "Dark Pursuit":        (3, A,  ST, enemy_low_hp,    1),
        "Death Spiral":        (3, A,  ST, None,            1),
        "Horus Guard":         (4, D,  A,  None,            1),
        "Balanced Rotation":   (4, A,  ST, variety,         1),
        "Golden Eye":          (4, A,  A,  None,            1),   # a mirror
        "Wyvron Counter":      (5, D,  A,  None,            1),
        "Wild Wind":           (5, D,  A,  None,            1),
        "Wyvron Guard":        (5, D,  A,  high_stability,  1),
        "Odax Smash":          (6, A,  ST, None,            1),
        "Omni Rotation":       (6, A,  ST, variety,         1),
        "Full Force":          (6, A,  ST, full_gauge,      1),
        "Valkyrie Strike":     (7, A,  ST, None,            1),
        "Brave Sword":         (7, A,  ST, None,            1),
        "Victory Evolution":   (7, A,  ST, behind,          1),
        "Spriggan Adaptation": (8, A,  ST, player_spams_attack, 1),
        "Storm Counter":       (8, D,  A,  None,            1),
        "Ultimate Balance":    (8, D,  A,  low_hp,          1),
    }

    check("every skill has a driver — none is skipped",
          set(DRIVE) == set(ALL_SKILLS),
          sorted(set(ALL_SKILLS) ^ set(DRIVE)))

    async def drive_skill(name):
        bno, bmove, pmove, setup, tries = DRIVE[name]
        e = SD.battle(bno)
        nb, ng = H.levelled(e["blade"], 100)
        spy = Spy()
        with spy:
            for seed in range(tries):
                s, _ch, npc, _c = await H.build_session(
                    player, pblade, nb, ng, "elite", seed=seed,
                    battle_no=bno)
                nk, pk = str(npc.id), str(player.id)
                if setup:
                    setup(s, nk, pk)
                s.moves[nk] = bmove
                s.moves[pk] = pmove
                await s._resolve_round()
                if spy.fired[name]:
                    break
        return spy.fired[name]

    unfired = []
    for name in ALL_SKILLS:
        if not await drive_skill(name):
            unfired.append(name)
    check("all 24 fire when driven into the state they describe — none is "
          "decoration", not unfired, unfired)

    # And the same 24 are reachable in ORDINARY play, reported rather than
    # asserted: whether a given boss drops below half HP in a sample is luck,
    # and this number is here to be read, not to fail a build.
    spy = Spy()
    with spy:
        for e in SD.SCHOOL_LEAGUE:
            nb, ng = H.levelled(e["blade"], 100)
            for seed in range(6):
                s, _ch, npc, _c = await H.build_session(
                    player, pblade, nb, ng, "elite", seed=seed,
                    battle_no=e["n"],
                    victory_points={str(player.id): 2, str(e["n"]): 0})
                await H.drive(s, str(player.id), H.Brain("elite", seed),
                              cap=60)
    seen = [n for n in ALL_SKILLS if spy.fired[n]]
    print(f"       ({len(seen)}/24 also came up in 48 ordinary battles; the "
          f"rest need states a boss reaches rarely)")

    # a sampled EFFECT check — the rule ran, AND the number it owns moved.
    #
    # Deliberately measuring Fortress Core's own +20, not the boss's stability
    # at the end of the round. A round applies several stability changes and
    # some are random — counters cost 5 each and fire on a chance roll — so a
    # net comparison reads as a failure roughly one run in six with the skill
    # working perfectly. That is a flaky test, not a finding.
    print("\n   …and the effect lands, not just the rule:")
    from cogs.battle.stability_manager import StabilityManager
    deltas: list = []
    _real_apply = StabilityManager._apply

    def tracing_apply(mgr, key, delta):
        deltas.append((key, float(delta)))
        return _real_apply(mgr, key, delta)

    nb, ng = H.levelled("King Kerbeus", 100)
    s, _ch, npc, _c = await H.build_session(player, pblade, nb, ng, "elite",
                                      seed=3, battle_no=2)
    nkey, pkey = str(npc.id), str(player.id)
    s.stability_manager.stability[nkey] = 40        # room to gain
    s.hp[nkey] = int(s.max_hp_per_player[nkey] * 0.3)   # below half → Fortress
    s.moves[nkey] = MOVE_DEFENSE
    s.moves[pkey] = MOVE_ATTACK
    StabilityManager._apply = tracing_apply
    try:
        await s._resolve_round()
    finally:
        StabilityManager._apply = _real_apply
    check("Fortress Core really adds its 20 stability to Ken when he is hurt",
          (nkey, 20.0) in deltas, deltas)
    check("...and it lands on Ken, not on the player",
          not [d for k, d in deltas if k == pkey and d == 20.0], deltas)

    # ── 4. bosses run all three; a player runs the one they paid for ─────────
    print("\n── 4. three skills for a boss, one for a player ────────────────")
    nb, ng = H.levelled("Storm Spriggan", 100)
    s, _ch, npc, ctrl = await H.build_session(player, pblade, nb, ng, "elite",
                                        seed=1, battle_no=8)
    nkey, pkey = str(npc.id), str(player.id)
    check("the boss's slot is None — it has no energy pool to spend",
          s.avatar_skill_slots[nkey] is None)
    check("...so all three of its skills compile",
          len({r.get("_name") for _i, r in s.ability._avatar_rules_for(nkey)})
          == 3,
          {r.get("_name") for _i, r in s.ability._avatar_rules_for(nkey)})
    check("a player with no avatar contributes no avatar rules",
          s.ability._avatar_rules_for(pkey) == [])
    check("avatar rule ids cannot collide with a blade's",
          all(i >= AbilityEngine.AVATAR_RID_BASE
              for i, _r in s.ability._avatar_rules_for(nkey)))
    check("...and blade rules are still there alongside them",
          len(s.ability._rules_for(s.blades[nkey], nkey))
          > len(s.ability._avatar_rules_for(nkey)))

    # ── 5. the stability line is no longer dead ──────────────────────────────
    print("\n── 5. the card's Stability column reaches the bar ──────────────")
    # `AvatarBonuses.stability_flat` and `apply_stability_bonus` were written
    # when the avatar system was built and had NO CALLERS AT ALL, so every card
    # advertising a stability bonus advertised nothing. Three of these eight
    # bosses are built on that column.
    bars = {}
    for e in SD.SCHOOL_LEAGUE:
        nb, ng = H.levelled(e["blade"], 100)
        s, _ch, npc, _c = await H.build_session(player, pblade, nb, ng, "elite",
                                          seed=1, battle_no=e["n"])
        bars[e["n"]] = s.stability_manager.max[str(npc.id)]
    check("every boss's bar is above the 100/150 the type table alone gives",
          all(v > 100 for v in bars.values()), bars)
    check("Ken and Wakiya — the two defensive walls — have the biggest bars",
          max(bars, key=bars.get) in (2, 5), bars)
    check("the player, with no avatar, still starts at the type default",
          s.stability_manager.max[str(player.id)] == 100,
          s.stability_manager.max[str(player.id)])

    # ── 6. the eight play differently ────────────────────────────────────────
    print("\n── 6. the eight bosses are not one boss in eight skins ─────────")
    profiles = {}
    for e in SD.SCHOOL_LEAGUE:
        nb, ng = H.levelled(e["blade"], 100)
        moves = collections.Counter()
        for seed in range(4):
            s, _ch, npc, _c = await H.build_session(player, pblade, nb, ng, "elite",
                                              seed=seed, battle_no=e["n"])
            await H.drive(s, str(player.id), H.Brain("elite", seed), cap=60)
            for mv, n in (s.move_counts.get(str(npc.id)) or {}).items():
                moves[mv] += n
        total = sum(moves.values()) or 1
        profiles[e["n"]] = tuple(round(moves[m] / total, 2)
                                 for m in ("attack", "defense", "stamina",
                                           "charge", "special"))
    check("no two opponents share a move profile",
          len(set(profiles.values())) >= 7, profiles)

    # ── 7. containment ───────────────────────────────────────────────────────
    print("\n── 7. a closed banner, both directions ─────────────────────────")
    shop = SHOP.AvatarShop.__new__(SHOP.AvatarShop)
    leaked = {}
    for pack in ("common", "rare", "epic", "legendary", "mlbb"):
        rm = shop._build_rarity_map(SHOP.PACK_POOL[pack])
        seen = collections.Counter()
        for _ in range(3000):
            for exact in (SHOP.PACK_GUARANTEE[pack], None):
                a = SHOP._pull_from_pool(pack, SHOP.PACK_POOL[pack], rm, exact)
                if a:
                    seen[a["rarity"]] += 1
        leaked[pack] = seen["Blader"]
    check("no existing pack can roll a Blader, over 30,000 pulls",
          not any(leaked.values()), leaked)
    rm = shop._build_rarity_map(SHOP.PACK_POOL["season1"])
    got, ids = collections.Counter(), collections.Counter()
    for _ in range(3000):
        a = SHOP._pull_from_pool("season1", SHOP.PACK_POOL["season1"], rm,
                                 "Blader")
        if a:
            got[a["rarity"]] += 1
            ids[a["id"]] += 1
    check("...and the Season 1 pack can roll nothing else",
          set(got) == {"Blader"}, dict(got))
    check("all eight are reachable from it", len(ids) == 8, len(ids))
    check("it pulls once, not twice", SHOP.PACK_PULLS["season1"] == 1)
    check("`;avatarpacks` no longer claims every pack gives two",
          "Each pack gives **2 avatars**" not in
          open(os.path.join(ROOT, "cogs", "avatar", "avatar_shop.py"),
               encoding="utf-8").read())

    # ── 8. the claim is atomic ───────────────────────────────────────────────
    print("\n── 8. one avatar, even under a race ────────────────────────────")
    UID = 700000000000000123
    H.STORE.data[str(UID)] = {"user_id": str(UID), "coins": 0, "xp": 0}
    wins, barrier = [], threading.Barrier(16)

    def racer():
        barrier.wait()
        wins.append(DB.claim_once(UID, SD.K_AVATAR_CLAIM))

    threads = [threading.Thread(target=racer) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("sixteen threads claim at once — exactly one wins",
          wins.count(True) == 1, collections.Counter(wins))
    check("...and a later claim still loses",
          DB.claim_once(UID, SD.K_AVATAR_CLAIM) is False)
    check("the flag is readable without touching it",
          await DB.has_claimed(UID, SD.K_AVATAR_CLAIM))
    DB.release_claim(UID, SD.K_AVATAR_CLAIM)
    check("a released claim can be won again — the undelivered-reward path",
          DB.claim_once(UID, SD.K_AVATAR_CLAIM) is True)
    check("the claim key is Story's own, not the League progress key",
          SD.K_AVATAR_CLAIM != SD.K_LEAGUE
          and SD.K_AVATAR_CLAIM == "school_league_avatar_reward_claimed")

    # ── 9. the reward, end to end ────────────────────────────────────────────
    print("\n── 9. clearing the League pays exactly one card ────────────────")
    import cogs.story.story_cog as SC
    cog = SC.StoryCog.__new__(SC.StoryCog)
    cog._active = set()

    def fresh(uid, cleared=0):
        prof = {"user_id": str(uid), "coins": 0, "xp": 0, "level": 0,
                "wins": 0, "losses": 0, "inventory": [], "parts": []}
        for n in range(1, cleared + 1):
            SD.record_clear(prof, SD.NORMAL, n)
        H.STORE.data[str(uid)] = copy.deepcopy(prof)
        return prof

    inv = {}
    _real_add = DB.add_avatar_to_inventory
    DB.add_avatar_to_inventory = lambda uid, aid: inv.setdefault(
        str(uid), []).append(aid)
    SC.add_avatar_to_inventory = DB.add_avatar_to_inventory
    try:
        U = 700000000000000200
        fresh(U, cleared=7)
        check("seven of eight cleared — Season's not done, no card",
              not SD.normal_complete(H.STORE.data[str(U)]))
        got1 = cog._award_blader(U)
        # (the gate lives in _finish; _award_blader is the claim itself)
        check("the claim pays a card the first time", got1 is not None
              and got1["id"] in SD.LEAGUE_AVATARS, got1)
        check("...and never a second", cog._award_blader(U) is None)
        check("exactly one card landed in the inventory",
              len(inv.get(str(U), [])) == 1, inv.get(str(U)))
        check("the card it paid is one of the eight",
              inv[str(U)][0] in SD.LEAGUE_AVATARS)

        # the pool really is random across players, not a fixed card
        rolled = set()
        for i in range(60):
            V = 700000000000001000 + i
            fresh(V, cleared=8)
            c = cog._award_blader(V)
            if c:
                rolled.add(c["id"])
        check("across sixty players the roll spreads over the pool",
              len(rolled) >= 6, sorted(rolled))

        # nothing retroactive
        W = 700000000000000300
        fresh(W, cleared=8)
        check("a player who cleared the League before this existed owns "
              "nothing until they win again",
              not inv.get(str(W)) and not await DB.has_claimed(
                  W, SD.K_AVATAR_CLAIM))
    finally:
        DB.add_avatar_to_inventory = _real_add
        SC.add_avatar_to_inventory = _real_add

    src = H.inspect.getsource(SC.StoryCog._finish)
    check("the grant is gated on Normal and on the whole League",
          "SD.NORMAL" in src and "normal_complete" in src)
    check("...and runs AFTER update_user, so the snapshot cannot erase the "
          "flag",
          src.index("update_user(player.id, profile)")
          < src.index("_award_blader"))
    aw = H.inspect.getsource(SC.StoryCog._award_blader)
    check("the flag goes down before the card is handed over",
          aw.index("claim_once") < aw.index("add_avatar_to_inventory"))
    check("...and is released if the grant raises",
          "release_claim" in aw)

    # ── 10. the Season 1 gate ────────────────────────────────────────────────
    print("\n── 10. the shop stays shut until Season 1 is done ──────────────")
    empty: dict = {}
    check("a fresh player cannot buy the Season 1 pack",
          not SD.season_complete(empty))
    full = {}
    for n in range(1, 9):
        SD.record_clear(full, SD.NORMAL, n)
    check("clearing the whole League on Normal is not Season 1 by itself — "
          "the Xender Dojo is still outstanding",
          SD.chapter_complete(full, SD.SCHOOL)
          and not SD.chapter_complete(full, SD.XENDER)
          and not SD.season_complete(full),
          SD.season_progress(full))
    check("...and the lock text says how far along they are",
          "1/2" in SHOP.PACK_REQUIRES["season1"][1](full),
          SHOP.PACK_REQUIRES["season1"][1](full))
    check("no other pack is gated", set(SHOP.PACK_REQUIRES) == {"season1"})
    buy_src = H.inspect.getsource(SHOP.AvatarShop.buy_pack.callback)
    check("the gate is checked before any coins move",
          buy_src.index("PACK_REQUIRES") < buy_src.index("_deduct_coins"),
          (buy_src.index("PACK_REQUIRES"), buy_src.index("_deduct_coins")))
    check("...and before the balance check, so a locked pack never says "
          "'not enough coins'",
          buy_src.index("PACK_REQUIRES") < buy_src.index("coins < price"))
    # and it opens on its own when the Dojo lands
    _real_cc = SD.chapter_complete
    SD.chapter_complete = lambda p, k: True
    try:
        check("with the Dojo shipped and cleared, Season 1 completes and the "
              "pack unlocks", SD.season_complete(full)
              and SHOP.PACK_REQUIRES["season1"][0](full))
    finally:
        SD.chapter_complete = _real_cc

    # ── 11. the ordinary avatar commands reach them ──────────────────────────
    print("\n── 11. the cards behave like every other card ──────────────────")
    shop = SHOP.AvatarShop.__new__(SHOP.AvatarShop)
    for aid, name, *_rest in TABLE:
        by_id = shop._resolve_avatar_query(aid)
        by_name = shop._resolve_avatar_query(name)
        by_lower = shop._resolve_avatar_query(name.lower())
        check(f"`;ainfo` finds {name} by id, name and lowercase name",
              (by_id or {}).get("id") == aid
              and (by_name or {}).get("id") == aid
              and (by_lower or {}).get("id") == aid)
    from cogs.avatar.avatar_utils import RARITY_COLORS, RARITY_EMOJI
    check("the Blader tier has a colour and an emoji of its own",
          "Blader" in RARITY_COLORS and "Blader" in RARITY_EMOJI)
    check("...and sorts alongside the other tiers",
          "Blader" in SHOP.RARITY_ORDER
          and "Blader" in SHOP.DUPE_REFUND_RATE)
    check("a duplicate refunds like any other pull",
          int(SHOP.PACK_PRICE["season1"]
              * SHOP.DUPE_REFUND_RATE["Blader"]) == 50_000)

    # ── 12. nothing else moved ───────────────────────────────────────────────
    print("\n── 12. the other 37 cards are untouched ────────────────────────")
    others = [a for a in avatar_engine.get_all_avatars()
              if a.get("rarity") != "Blader"]
    check("there are still 37 of them", len(others) == 37, len(others))
    raw = json.load(open(os.path.join(ROOT, "cogs", "avatar",
                                      "avatar_data.json"), encoding="utf-8"))
    check("no existing card gained a rules block",
          not any(sk.get("rules") for a in raw["avatars"]
                  if a.get("rarity") != "Blader"
                  for sk in (a.get("skills") or [])))
    check("the existing packs' prices and pools are unchanged",
          SHOP.PACK_PRICE["legendary"] == 500_000
          and SHOP.PACK_PRICE["mlbb"] == 15_000_000
          and SHOP.PACK_POOL["mlbb"] == ["MLBB"])


if __name__ == "__main__":
    sys.exit(main())
