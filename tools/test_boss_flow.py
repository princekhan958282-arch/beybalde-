"""Focused boss flow regressions. Run with: python -m unittest tools.test_boss_flow"""
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.battle.boss import boss_ai as ai
from cogs.battle.boss import boss_battle as bb
from cogs.battle.boss import boss_card as card
from cogs.battle.boss import boss_card_pillow as pillow
from cogs.battle.boss import boss_tiers as tiers
from cogs.battle.boss import blade_abilities as bk


def member(uid):
    return SimpleNamespace(id=uid, display_name=f"Player {uid}", mention=f"<@{uid}>")


class BossFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_cure_support_applies_to_allies_and_restores_stats(self):
        blades = json.loads((Path(__file__).resolve().parents[1] /
                             "data/beyblades.json").read_text())
        for name, stat in (("Cure Black", "attack"), ("Cure White", "defense")):
            fight = self.make_fight()
            fight.kits[2] = bk.kit_for(blades[name])
            observed = []
            def resolve(boss, actor, *moves):
                observed.append((actor.attack, actor.defense))
                return {"dmg_to_a": 0.0, "dmg_to_b": 0.0, "heal_a": 0.0, "heal_b": 0.0}
            with (patch.object(ai, "resolve", side_effect=resolve),
                  patch.object(ai, "choose_move", return_value=(ai.MOVE_ATTACK, {}))):
                fight.step(ai.MOVE_ATTACK)
                self.assertEqual(observed[-1], (156, 120) if stat == "attack" else (120, 156))
                self.assertEqual(fight.fighters[1].attack, 120)
                self.assertEqual(fight.fighters[1].defense, 120)
                fight.step(ai.MOVE_ATTACK)
                self.assertEqual(observed[-1], (120, 120))  # No self buff.
                fight.fighters[2].hp = 0
                fight.step(ai.MOVE_ATTACK)
                self.assertEqual(observed[-1], (120, 120))

    def make_fight(self):
        party = [member(1), member(2)]
        fighters = {
            m.id: ai.Fighter(m.display_name, 2000, 2000, 120, 120, 120,
                             sp=15) for m in party
        }
        kits = {m.id: bk.BladeKit({}) for m in party}
        fight = bb.BossFight(party[0], "drakos", party=party,
                             _fighters=fighters, _kits=kits,
                             _blades={m.id: {} for m in party})
        return fight

    def test_boss_damage_uses_target_actual_defense_and_rotates(self):
        fight = self.make_fight()
        fight.fighters[1].defense = 500
        fight.fighters[2].defense = 30
        with patch.object(ai, "choose_move", return_value=(ai.MOVE_ATTACK, {})):
            fight.step(ai.MOVE_DEFENSE)  # First player; boss sets up.
            before_1, before_2 = fight.fighters[1].hp, fight.fighters[2].hp
            report = fight.step(ai.MOVE_ATTACK)  # Boss hits the second actor.
            self.assertEqual(fight.fighters[1].hp, before_1)
            self.assertLess(fight.fighters[2].hp, before_2)
            self.assertEqual(report["actor"].id, 2)
            self.assertEqual(fight.active.id, 2)  # Next round starts rotated.
            self.assertEqual(fight.card_state()["round"], 2)
            fight.step(ai.MOVE_DEFENSE)
            self.assertEqual(fight.active.id, 1)

    async def test_failed_preflight_releases_all_lobby_members(self):
        party = [member(1), member(2)]
        cog = SimpleNamespace(_active={1, 2})
        lobby = bb.BossLobbyView(cog, party[0], "drakos")
        lobby.party = party
        with patch.object(bb.BossFight, "create", new_callable=AsyncMock,
                          side_effect=ValueError("bad equipment")):
            await lobby._launch(None)
        self.assertFalse(cog._active)
        self.assertFalse(lobby._paid)

    async def test_reward_receipt_prevents_duplicate_coin_grant(self):
        fight = self.make_fight()
        fight.party = fight.party[:1]
        fight.result = "win"
        fight.finished = True
        profile = {"coins": 0, "bosses_cleared": [], "active_beyblade": None}

        async def mutate(_uid, fn):
            return fn(profile)

        cog = bb.BossCog(SimpleNamespace())
        cog._active.add(1)
        channel = SimpleNamespace(send=AsyncMock())
        with (patch.object(bb, "mutate_user", side_effect=mutate),
              patch.object(bb, "grant_xp"),
              patch.object(bb.binfo, "REGISTRY", {}),
              patch.object(bb.casino_wallet, "credit", new_callable=AsyncMock) as credit,
              patch.object(bb.gemini, "say_with_deadline", new_callable=AsyncMock,
                           return_value="Try again")):
            await cog.finish(fight, channel)
            first_coins = profile["coins"]
            await cog.finish(fight, channel)
        self.assertGreater(first_coins, 0)
        self.assertEqual(profile["coins"], first_coins)
        self.assertEqual(credit.await_count, 1)
        self.assertEqual(profile["bosses_cleared"], ["drakos"])

    def test_argus_lobby_quotes_actual_stats_and_entry(self):
        state = bb.lobby_card_state("argus", [member(1), member(2)],
                                    tier="nightmare")
        stats = dict(state["stats"])
        base_atk, base_def, base_sta = bb.boss_stats(bb.BOSSES["argus"])
        self.assertEqual(stats["Defense"], f"{base_def:.0f}")
        self.assertEqual(stats["Stamina"], f"{base_sta:.0f}")
        self.assertEqual(stats["Attack"], f"{tiers.scale_attack(base_atk * 1.05, 'nightmare'):.0f}")
        self.assertIn("1,000,000", stats["Entry"])
        self.assertIn("1,000,000", tiers.summary_line("nightmare", "argus"))
        self.assertIn("boss copy", stats["Reward"])
        self.assertEqual(state["tier"], "Nightmare")
        self.assertIsNotNone(pillow.render_lobby_pillow(state))

    def test_battle_card_shows_tier_round_and_special_warning(self):
        fight = self.make_fight()
        fight.boss.gauge = ai.SPECIAL_GAUGE_MAX
        state = fight.card_state()
        html = card.build_html(state)
        self.assertIn("BOSS SPECIAL READY", html)
        self.assertIn("ROUND 1", html)
        self.assertIn("Standard", html)
        self.assertIsNotNone(pillow.render_battle_pillow(state))


if __name__ == "__main__":
    unittest.main()
