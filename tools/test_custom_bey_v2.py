"""Custom builder: real battle ops, V2 serialization, channel persistence and images."""
import asyncio
import copy
import io
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image
import discord
from utils.custom_bey import ABILITY_PRESETS, ABILITY_BUDGET, CUSTOM_TRIGGERS, allowed_triggers, build, CustomBeyError
from cogs.custom_bey import CustomBeyBuilder, CustomApprovalView, AbilityCatalogView, validate_image_bytes, BuilderTextModal, CustomBeyCog, public_image_url
from tools.test_october_bey_roster import build as battle, P, E


def blade(keys, triggers=None):
    return build('Test Custom', 'Attack', 100,100,100,95,keys,'Custom Strike',120,'none',
                 'https://example.com/bey.png',triggers or [allowed_triggers(k)[0] for k in keys])


class Rules(unittest.TestCase):
    def test_catalog_exactly_forty_added_and_budget(self):
        self.assertEqual(len(ABILITY_PRESETS),56)
        self.assertEqual(ABILITY_BUDGET,100)
        self.assertEqual(blade(['extra_special_hit','deep_drain'])['custom_meta']['ability_cost'],100)
        self.assertEqual({cfg['cost'] for cfg in ABILITY_PRESETS.values()}, {50})
        self.assertEqual(blade(['extra_special_hit','second_wind'])['custom_meta']['ability_cost'],100)
        self.assertEqual(blade(['heal'])['custom_meta']['ability_cost'],50)
        with self.assertRaises(CustomBeyError): blade(['heal','shield','true_damage'])
        with self.assertRaises(CustomBeyError): blade(['heal','heal'])
        with self.assertRaises(CustomBeyError): blade(['bonus_damage'],['turn_end'])
        with self.assertRaises(CustomBeyError):
            build('Test Custom','Attack',100,100,100,95,[],'Special',120,'none')

    def test_every_effect_executes_in_real_battle_engine_and_cooldown_blocks_repeat(self):
        for key in ABILITY_PRESETS:
            with self.subTest(effect=key):
                s,_=battle('Dranzer')
                b=blade([key]); s.blades[P]=b
                s.hp[E]=200; s.hp[P]=200
                logs=[]
                if key == 'cleanse':
                    s.status.add_buff(P,'attack',-10,2)
                def state():
                    return copy.deepcopy((s.hp,s.stamina_manager.stamina,s.stability_manager.stability,s.status.snapshot(P),s.status.snapshot(E),
                        s.ability.crit_chance_bonus,s.ability.crit_damage_mult,s.ability.reflect_windows,
                        s.ability.resist_windows,s.ability.lifesteal_pct,s.ability.revive_pool_pct,
                        s.ability.hp_regen_per_turn,s.ability.primed_bonus,s.ability.primed_hit_bonus,
                        s.ability.extra_special_hits,s.ability.counters))
                before=state()
                trigger=b['abilities'][0]['trigger']
                damage,_=s.ability._fire(trigger,P,E,b,'attack','win',100,0,logs)
                # The engine sets this only after executing the preset operation.
                self.assertEqual(s.ability.cooldowns.get((P,f'custom_{key}')),3)
                second=[]
                s.ability._fire(trigger,P,E,b,'attack','win',100,0,second)
                self.assertEqual(second,[])
                self.assertTrue(damage != 100 or before != state(), f'{key} did not change damage or battle state')

    def test_thresholds_and_revival_once(self):
        s,_=battle('Dranzer'); b=blade(['heal'],['low_hp']); s.blades[P]=b
        s.hp[P]=s.max_hp_per_player[P]
        s.ability._fire('on_low_hp',P,E,b,'attack','win',0,0,[])
        self.assertNotIn((P,'custom_heal'),s.ability.cooldowns)
        s.hp[P]=100
        s.ability._fire('on_low_hp',P,E,b,'attack','win',0,0,[])
        self.assertEqual(s.hp[P],118)
        s,_=battle('Dranzer'); b=blade(['second_wind']); s.blades[P]=b
        s.ability._fire('turn_start',P,E,b,'attack','win',0,0,[])
        s.ability.revive_pool_pct[P]=0
        s.ability.cooldowns.clear()
        s.ability._fire('turn_start',P,E,b,'attack','win',0,0,[])
        self.assertEqual(s.ability.revive_pool_pct[P],0)


class Panel(unittest.IsolatedAsyncioTestCase):
    async def test_v2_paging_budget_and_persistent_review(self):
        builder=CustomBeyBuilder(1,'Attack')
        self.assertIsInstance(builder,discord.ui.LayoutView)
        self.assertEqual(builder.to_components()[0]['type'],17)
        shown=[]
        for page in range(3):
            builder.mode='ability'; builder.effect_page=page; builder.rebuild()
            opts=builder.rows[0].children[0].options
            self.assertLessEqual(len(opts),25)
            shown.extend(x.value for x in opts)
        self.assertEqual(shown,list(ABILITY_PRESETS))
        self.assertTrue(CustomApprovalView(0,1).is_persistent())
        for page in range(6):
            view=AbilityCatalogView(1,page)
            self.assertLess(len(str(view.to_components())),10000)
        builder.abilities[1]['effect']='second_wind'
        i=MagicMock(); i.data={'values':['second_wind']}; i.response.send_message=AsyncMock()
        await builder.pick_effect(i)
        self.assertIsNone(builder.abilities[0]['effect'])
        i.response.send_message.assert_awaited_once()
        i.data={'values':['extra_special_hit']}; i.response.edit_message=AsyncMock()
        await builder.pick_effect(i)
        self.assertEqual(builder.used_points(),100)
        self.assertEqual(builder.abilities[0]['effect'],'extra_special_hit')

    async def test_image_requires_transparency_and_visible_pixels(self):
        for color, okay in [((0,0,0,0),False),((0,0,0,255),False)]:
            image=Image.new('RGBA',(10,10),color); data=io.BytesIO(); image.save(data,format='PNG')
            with self.assertRaises(CustomBeyError): await validate_image_bytes(data.getvalue())
        image.putpixel((0,0),(0,0,0,0)); data=io.BytesIO(); image.save(data,format='PNG')
        await validate_image_bytes(data.getvalue())
        with self.assertRaises(CustomBeyError): await validate_image_bytes(b'not an image')

    async def test_image_urls_reject_local_addresses(self):
        for url in ('http://127.0.0.1/a.png','http://[::1]/a.png','http://169.254.169.254/a.png','file:///a.png'):
            with self.assertRaises(CustomBeyError): public_image_url(url)
        public_image_url('https://cdn.discordapp.com/attachments/bey.png')

    async def test_channel_config_persists_and_command_sends_reviews_to_target(self):
        from utils import database as db
        with tempfile.TemporaryDirectory() as temp, patch.object(db,'CONFIG_PATH',temp+'/config.json'):
            db.set_custom_review_channel(123,456)
            self.assertEqual(db.get_custom_review_channel(123),456)
            self.assertEqual(db.get_custom_review_channel(124),456)
            db.set_custom_review_channel(789,999)
            self.assertEqual(db.get_custom_review_channel(123),999)
            self.assertEqual(db.get_custom_review_channel(124),999)
        ctx=MagicMock(); ctx.guild.id=123; ctx.send=AsyncMock()
        target=MagicMock(); target.guild.id=123; target.id=456; target.mention='<#456>'; target.send=AsyncMock()
        with patch('cogs.custom_bey.set_custom_review_channel') as save, patch('cogs.custom_bey._pending_custom_submissions',AsyncMock(return_value=[(1,{**blade(['heal']),'submitted_guild_id':123}),(2,{**blade(['heal']),'submitted_guild_id':124})])):
            await CustomBeyCog.setcustom.callback(CustomBeyCog(None),ctx,target)
            save.assert_called_once_with(123,456)
            self.assertEqual(target.send.await_count,2)

    async def test_single_legacy_review_channel_works_for_other_servers(self):
        from utils import database as db
        with patch.object(db,'load_config',return_value={'123':{'custom_review_channel_id':456}}):
            self.assertEqual(db.get_custom_review_channel(124),456)
        with patch.object(db,'load_config',return_value={}):
            self.assertIsNone(db.get_custom_review_channel(124))

    async def test_submission_from_other_server_uses_central_channel(self):
        b=CustomBeyBuilder(1,'Attack')
        b.draft.update(name='Test Custom',stats=(100,100,100,95),image='https://example.com/bey.png',
                       special_name='Custom Strike',special_damage=120,special_effect='none')
        b.abilities[0].update(name='Heal',effect='heal',trigger=allowed_triggers('heal')[0])
        i=MagicMock(); i.user.id=1; i.guild.id=124
        i.response.defer=AsyncMock(); i.edit_original_response=AsyncMock()
        target=MagicMock(); target.send=AsyncMock()
        i.client.get_channel.return_value=None
        i.client.fetch_channel=AsyncMock(return_value=target)
        saved={}
        async def mutate(uid, callback): callback(saved)
        with patch('cogs.custom_bey.get_custom_review_channel',return_value=456), \
                patch('cogs.custom_bey.validate_image_url',AsyncMock()), \
                patch('cogs.custom_bey.get_beyblade',return_value=None), \
                patch('cogs.custom_bey.mutate_user',side_effect=mutate):
            await b.create(i)
        i.client.fetch_channel.assert_awaited_once_with(456)
        i.guild.get_channel.assert_not_called()
        target.send.assert_awaited_once()
        self.assertEqual(saved['custom_bey']['submitted_guild_id'],124)
        self.assertEqual(saved['custom_bey']['approval_status'],'pending')

    async def test_create_opens_in_another_server_and_review_remains_owner_only(self):
        i=MagicMock(); i.guild.id=124; i.user.id=1
        i.response.send_message=AsyncMock(); i.original_response=AsyncMock()
        with patch('cogs.custom_bey.get_custom_review_channel',return_value=456), \
                patch('cogs.custom_bey.get_user',AsyncMock(return_value={})):
            await CustomBeyCog.custombey.callback(CustomBeyCog(i.client),i,
                discord.app_commands.Choice(name='Create',value='create'),
                discord.app_commands.Choice(name='Attack',value='Attack'))
        self.assertIsInstance(i.response.send_message.call_args.kwargs['view'],CustomBeyBuilder)
        i.client.is_owner=AsyncMock(return_value=False)
        self.assertFalse(await CustomApprovalView(0,1).interaction_check(i))

    async def test_image_modal_updates_original_builder(self):
        b=CustomBeyBuilder(1,'Attack'); b._message=MagicMock(); b._message.edit=AsyncMock()
        modal=BuilderTextModal(b,'image_url'); modal.value._value='https://example.com/bey.png'
        i=MagicMock(); i.user.id=1; i.response.defer=AsyncMock()
        with patch('cogs.custom_bey.validate_image_url',AsyncMock()): await modal.on_submit(i)
        b._message.edit.assert_awaited_once()
        self.assertEqual(b.draft['image'],'https://example.com/bey.png')


if __name__=='__main__': unittest.main()
