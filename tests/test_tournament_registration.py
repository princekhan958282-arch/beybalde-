"""Registration flows use real V2 components with mocked Discord network calls."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from cogs.tournament.drafts import DraftConfig, DraftStore
from cogs.tournament.host_panel import HostPanel, SettingModal, RegistrationChannelSelect
from cogs.tournament.registration import Registration, RegistrationPanel, RegistrationStore, RegButton, validate_channel
from cogs.tournament.tournament import TournamentCog, MASTER_ID

pytestmark = pytest.mark.asyncio

def actor(uid=MASTER_ID):
    return NS(id=uid, roles=[], bot=False)

def click(uid=MASTER_ID, guild=None):
    return NS(user=actor(uid), guild_id=1, guild=guild, response=NS(
        send_message=AsyncMock(), edit_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock(), is_done=lambda: False),
        followup=NS(send=AsyncMock()), edit_original_response=AsyncMock())

def text(view):
    return '\n'.join(x.content for x in view.walk_children() if isinstance(x, discord.ui.TextDisplay))

@pytest.fixture
def cog(tmp_path, monkeypatch):
    bot = NS(is_owner=AsyncMock(return_value=False), add_view=Mock(), get_partial_messageable=Mock())
    c = TournamentCog(bot)
    c.draft_store = DraftStore(tmp_path / 'drafts.db')
    c.registration_store = RegistrationStore(c.draft_store)
    c._in_battle = lambda uid: False
    monkeypatch.setattr('cogs.tournament.registration.get_user', AsyncMock(return_value={'inventory': ['test']}))
    monkeypatch.setattr('cogs.battle.boss.boss_copy.equipped_blade', AsyncMock(return_value=({'name': 'test'}, None)))
    monkeypatch.setattr('cogs.tournament.tournament.draft_pool', lambda: [{'name': 'test'}])
    return c

@pytest.fixture
def guild():
    g = NS(id=1, me=actor(100))
    ch = Mock(spec=discord.TextChannel)
    ch.id, ch.guild = 2, g
    ch.permissions_for.return_value = NS(view_channel=True, send_messages=True, read_message_history=True)
    msg = NS(id=3, jump_url='https://discord.com/channels/1/2/3', edit=AsyncMock())
    ch.send = AsyncMock(return_value=msg)
    ch.get_partial_message = Mock(return_value=msg)
    g.get_channel = lambda uid: ch if uid == 2 else None
    return g

async def host(cog):
    p = HostPanel(cog, 1, MASTER_ID)
    p.message = NS(edit=AsyncMock())
    cog.setups[1] = p
    return p

async def published(cog, guild, config=None):
    p = await host(cog)
    p.config = config or DraftConfig()
    await p.act(click(guild=guild), 'confirm')
    await p.act(click(guild=guild), 'channel', 2)
    await p.act(click(guild=guild), 'publish')
    return cog.registrations[1], p

async def test_confirm_channel_publish(cog, guild):
    p = await host(cog)
    await p.act(click(guild=guild), 'confirm')
    assert not p.closed and p.channel_stage
    assert any(isinstance(x, RegistrationChannelSelect) for x in p.walk_children())
    assert not cog.registrations and p.total_children_count <= 40
    await p.act(click(guild=guild), 'channel', 2)
    assert p.channel_id == 2
    await p.act(click(guild=guild), 'publish')
    reg = cog.registrations[1]
    assert p.closed and p.published and not cog.setups
    assert reg.reg.status == 'open'
    assert 'https://discord.com/channels/1/2/3' in text(p)
    assert {x.label for x in reg.walk_children() if isinstance(x, RegButton)} == {'Join', 'Leave', 'Cancel Registration'}
    assert 'Avatars' in text(reg) and 'Equipped Beyblade' in text(reg)
    assert cog.registration_store.current(1).message_id == 3
    assert not cog.admin_start(1)

@pytest.mark.parametrize('permission', ['view_channel', 'send_messages', 'read_message_history'])
async def test_channel_permissions(cog, guild, permission):
    p = await host(cog)
    await p.act(click(guild=guild), 'confirm')
    setattr(guild.get_channel(2).permissions_for.return_value, permission, False)
    i = click(guild=guild)
    await p.act(i, 'channel', 2)
    assert p.channel_id is None
    assert i.response.send_message.call_args.kwargs['ephemeral']
    assert permission.split('_')[0].lower() in i.response.send_message.call_args.args[0].lower()

async def test_publish_rechecks_permissions_and_duplicate(cog, guild):
    p = await host(cog)
    await p.act(click(guild=guild), 'confirm')
    await p.act(click(guild=guild), 'channel', 2)
    guild.get_channel(2).permissions_for.return_value.send_messages = False
    i = click(guild=guild)
    await p.act(i, 'publish')
    assert i.followup.send.call_args.kwargs['ephemeral']
    assert not p.closed and not cog.registrations
    guild.get_channel(2).permissions_for.return_value.send_messages = True
    await p.act(click(guild=guild), 'publish')
    sender = AsyncMock()
    await cog.open_panel(sender, guild, actor(), guild.get_channel(2))
    assert sender.call_args.kwargs['ephemeral']
    reg = Registration(1, MASTER_ID, 2, DraftConfig())
    with pytest.raises(ValueError):
        cog.registration_store.begin(reg)

async def test_failed_send_releases_reservation(cog, guild):
    p = await host(cog)
    p.channel_id = 2
    guild.get_channel(2).send.side_effect = RuntimeError('network failed')
    with pytest.raises(RuntimeError):
        await cog.publish_registration(p, guild)
    assert not cog.registrations
    assert cog.registration_store.current(1) is None

async def test_join_leave_duplicate_full_and_ownership(cog, guild):
    panel, _ = await published(cog, guild, DraftConfig(slots=4))
    button = next(x for x in panel.walk_children() if isinstance(x, RegButton) and x.action == 'join')
    await button.callback(click(10))
    assert panel.reg.entrants == [10] and 10 in cog._active
    assert 'Players (1/4)' in text(panel)
    assert cog.registration_store.current(1).entrants == [10]
    i = click(10)
    await panel.act(i, 'join')
    assert i.followup.send.call_args.kwargs['ephemeral']
    await asyncio.gather(*(panel.act(click(uid), 'join') for uid in (11, 12, 13, 14)))
    assert len(panel.reg.entrants) == 4
    assert 'Players (4/4)' in text(panel)
    assert not cog.lobbies
    await panel.act(click(10), 'leave')
    assert 10 not in cog._active and 10 not in panel.reg.entrants
    await panel.act(click(14), 'join')
    assert 14 in panel.reg.entrants

@pytest.mark.parametrize('reason', ['banned', 'busy', 'no_inventory', 'no_equipped', 'bot', 'wrong_guild'])
async def test_join_refusals(cog, guild, monkeypatch, reason):
    panel, _ = await published(cog, guild)
    i = click(10)
    if reason == 'banned': cog.banned.add(10)
    if reason == 'busy': cog._in_battle = lambda _: True
    if reason == 'no_inventory': monkeypatch.setattr('cogs.tournament.registration.get_user', AsyncMock(return_value={'inventory': []}))
    if reason == 'no_equipped': monkeypatch.setattr('cogs.battle.boss.boss_copy.equipped_blade', AsyncMock(return_value=(None, None)))
    if reason == 'bot': i.user.bot = True
    if reason == 'wrong_guild': i.guild_id = 9
    await panel.act(i, 'join')
    assert panel.reg.entrants == []
    assert i.response.send_message.await_count + i.followup.send.await_count == 1

async def test_random_does_not_require_equipped_and_fees_not_charged(cog, guild, monkeypatch):
    panel, _ = await published(cog, guild, DraftConfig(bey_selection='Random Beyblade', entry_fee=5000, avatars='Disabled'))
    equipped = AsyncMock(return_value=(None, None))
    monkeypatch.setattr('cogs.battle.boss.boss_copy.equipped_blade', equipped)
    await panel.act(click(10), 'join')
    equipped.assert_not_awaited()
    assert panel.reg.entrants == [10]
    assert '5,000 Beycoins' in text(panel) and 'not charged' in text(panel)
    assert '**Avatar Level:**' not in text(panel)

async def test_cancel_requires_host_confirmation_and_releases(cog, guild):
    panel, _ = await published(cog, guild)
    await panel.act(click(10), 'join')
    i = click(10)
    await panel.act(i, 'cancel')
    assert i.response.send_message.call_args.kwargs['ephemeral']
    i = click()
    await panel.act(i, 'cancel')
    modal = i.response.send_modal.call_args.args[0]
    modal.confirmation._value = 'NO'
    await modal.on_submit(click())
    assert panel.reg.status == 'open'
    modal.confirmation._value = 'CANCEL'
    await modal.on_submit(click())
    assert panel.reg.status == 'cancelled' and not cog.registrations and not cog._active
    assert cog.registration_store.current(1) is None
    assert not any(isinstance(x, RegButton) for x in panel.walk_children())
    panel2, _ = await published(cog, guild)
    await panel2.act(click(10), 'join')
    await modal.on_submit(click())  # stale modal must not release the new registration
    assert cog.registrations[1] is panel2 and 10 in cog._active

async def test_restart_restores_players_and_persistent_controls(cog, guild):
    panel, _ = await published(cog, guild)
    await panel.act(click(10), 'join')
    bot = NS(is_owner=AsyncMock(return_value=False), add_view=Mock(), get_partial_messageable=Mock(return_value=guild.get_channel(2)))
    restored = TournamentCog(bot)
    restored.draft_store = cog.draft_store
    restored.registration_store = RegistrationStore(restored.draft_store)
    restored._in_battle = lambda _: False
    await restored.cog_load()
    view = restored.registrations[1]
    assert view.is_persistent() and view.reg.entrants == [10] and 10 in restored._active
    bot.add_view.assert_called_once_with(view, message_id=3)
    await view.act(click(10), 'leave')
    assert not view.reg.entrants
    await restored.cog_unload()
    assert cog.registration_store.current(1) is not None

async def test_avatar_level_limits_and_hidden_controls(cog):
    p = await host(cog)
    p.config = DraftConfig(avatars='Disabled')
    p.build()
    assert 'Avatar Level' not in text(p)
    assert p.total_children_count <= 40
    p.config = DraftConfig()
    for level in (0, 6, -1):
        m = SettingModal(p, 'avatar_level')
        m.input._value = str(level)
        i = click()
        await m.on_submit(i)
        assert i.response.send_message.call_args.kwargs['ephemeral']
    m = SettingModal(p, 'avatar_level')
    m.input._value = '5'
    await m.on_submit(click())
    assert p.config.avatar_level_rule == 'Equalized Level' and p.config.avatar_level == 5
    assert 'Equalized Level 5' in text(p)
    assert p.total_children_count <= 40
    p.config = p.config.updated('avatars', 'Disabled')
    assert p.config.avatar_level == 5

async def test_legacy_draft_migration(cog):
    import json
    from dataclasses import asdict
    legacy = {key: value for key, value in asdict(DraftConfig()).items() if key not in ('avatars', 'avatar_level_rule', 'avatar_level', 'bey_selection')}
    with cog.draft_store._connect() as conn:
        conn.execute('INSERT INTO drafts VALUES (?, ?, ?, ?)', (1, MASTER_ID, json.dumps(legacy), 'draft'))
    loaded = cog.draft_store.load(1)[1]
    assert loaded.avatars == 'Allowed' and loaded.bey_selection == 'Equipped Beyblade'

async def test_cross_guild_membership_and_ban(cog, guild):
    panel, _ = await published(cog, guild)
    await panel.act(click(10), 'join')
    reg = Registration(2, MASTER_ID, 22, DraftConfig(), status='open', message_id=33)
    cog.registration_store.begin(reg)
    with pytest.raises(ValueError, match='another'):
        cog.registration_store.membership(reg, 10, True)
    await panel.remove_banned(10)
    assert not panel.reg.entrants and 10 not in cog._active

async def test_incomplete_publication_recovered(cog):
    reg = Registration(1, MASTER_ID, 2, DraftConfig())
    cog.registration_store.begin(reg)
    assert cog.registration_store.restore() == []
    assert cog.registration_store.current(1) is None

async def test_channel_select_callback(cog, guild):
    p = await host(cog)
    await p.act(click(guild=guild), 'confirm')
    select = next(x for x in p.walk_children() if isinstance(x, RegistrationChannelSelect))
    select._values = [NS(id=2)]
    await select.callback(click(guild=guild))
    assert p.channel_id == 2

async def test_no_random_pool_and_wrong_channel(cog, guild, monkeypatch):
    p = await host(cog)
    p.channel_id = 2
    p.config = DraftConfig(bey_selection='Random Beyblade')
    monkeypatch.setattr('cogs.tournament.tournament.draft_pool', lambda: [])
    with pytest.raises(ValueError, match='eligible'):
        await cog.publish_registration(p, guild)
    assert not cog.registrations
    with pytest.raises(ValueError, match='text channel'):
        validate_channel(guild, None)
    channel = guild.get_channel(2)
    channel.guild = NS(id=9)
    with pytest.raises(ValueError, match='this server'):
        validate_channel(guild, channel)

async def test_reload_blocks_stale_cancellation_modal(cog, guild):
    panel, _ = await published(cog, guild)
    i = click()
    await panel.act(i, 'cancel')
    modal = i.response.send_modal.call_args.args[0]
    modal.confirmation._value = 'CANCEL'
    await cog.cog_unload()
    assert not cog.registrations
    await modal.on_submit(click())
    assert cog.registration_store.current(1).status == 'open'

async def test_cancel_write_failure_and_stale_publish(cog, guild, monkeypatch):
    panel, p = await published(cog, guild)
    monkeypatch.setattr(cog.registration_store, 'write', Mock(side_effect=OSError('disk unavailable')))
    with pytest.raises(OSError):
        await panel.close()
    assert panel.reg.status == 'open' and cog.registrations[1] is panel
    i = click(guild=guild)
    await p.act(i, 'publish')
    assert i.response.send_message.call_args.kwargs['ephemeral']
    assert guild.get_channel(2).send.await_count == 1
