"""Headless interaction tests using real discord.py Components V2."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import asyncio
import discord
import pytest
from cogs.tournament.drafts import DraftConfig, DraftStore, LABELS, OPTIONS
from cogs.tournament.host_panel import HostPanel, EditSelect, SettingSelect, SettingModal, PanelButton
from cogs.tournament.tournament import TournamentCog, MASTER_ID, ADMIN_ROLE
from cogs.tournament.hosters import TOURNAMENT_HOSTER_IDS

pytestmark = pytest.mark.asyncio

def user(uid=MASTER_ID, roles=()):
    return NS(id=uid, roles=[NS(name=r) for r in roles])

def interaction(who=None):
    response = NS(send_message=AsyncMock(), edit_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock(), is_done=lambda: False)
    return NS(user=who or user(), response=response, edit_original_response=AsyncMock(), followup=NS(send=AsyncMock()))

@pytest.fixture
def cog(tmp_path):
    c = TournamentCog(NS(is_owner=AsyncMock(return_value=False)))
    c.draft_store = DraftStore(tmp_path / 'drafts.db')
    return c

async def opened(c, who=None):
    send = AsyncMock(return_value=NS(edit=AsyncMock()))
    await c.open_panel(send, NS(id=1), who or user(), NS(id=2))
    return c.setups.get(1), send

def text(panel):
    return '\n'.join(item.content for item in panel.walk_children() if isinstance(item, discord.ui.TextDisplay))

async def test_defaults_and_v2(cog):
    p, send = await opened(cog)
    assert isinstance(p, discord.ui.LayoutView)
    assert len(p.to_components()) == 1
    assert p.content_length() < 4000
    assert all(label in text(p) for label in LABELS.values())
    assert 'First to 3' in text(p) and '0 Beycoins' in text(p)
    assert send.call_args.kwargs == {'view': p}
    assert not cog.lobbies
    assert {b.label for b in p.walk_children() if isinstance(b, PanelButton)} == {'Confirm Setup', 'Reset', 'Cancel Setup'}

@pytest.mark.parametrize('who,allowed', [(user(), True), (user(next(iter(TOURNAMENT_HOSTER_IDS))), True), (user(6, [ADMIN_ROLE]), True), (user(7), False)])
async def test_open_permissions(cog, who, allowed):
    p, send = await opened(cog, who)
    assert (p is not None) == allowed
    if not allowed:
        assert send.call_args.kwargs['ephemeral']

async def test_bot_owner(cog):
    cog.bot.is_owner.return_value = True
    p, _ = await opened(cog, user(19))
    assert p
    assert await p.interaction_check(interaction(user(19)))

async def test_host_only_and_revoked_permissions(cog):
    p, _ = await opened(cog, user(6, [ADMIN_ROLE]))
    assert not await p.interaction_check(interaction(user()))
    assert not await p.interaction_check(interaction(user(6)))
    assert await p.interaction_check(interaction(user(6, [ADMIN_ROLE])))

@pytest.mark.parametrize('key', OPTIONS)
async def test_every_enum_and_preview(cog, key):
    p, _ = await opened(cog)
    for value in OPTIONS[key]:
        i = interaction()
        edit = next(x for x in p.walk_children() if isinstance(x, EditSelect))
        edit._values = [key]
        await edit.callback(i)
        select = next(x for x in p.walk_children() if isinstance(x, SettingSelect))
        assert [o.value for o in select.options] == [str(v) for v in OPTIONS[key]]
        select._values = [str(value)]
        await select.callback(interaction())
        assert getattr(p.config, key) == value
        assert p.config.display(key) in text(p)
        assert p.editing is None
        assert any(isinstance(x, EditSelect) for x in p.walk_children())
        i.response.edit_message.assert_awaited_once()
        assert p.config.name == DraftConfig().name

@pytest.mark.parametrize('key,raw,expected', [('name', 'My Championship', 'My Championship'), ('entry_fee', '2500', 2500), ('entry_fee', '0', 0)])
async def test_modal_updates_original(cog, key, raw, expected):
    p, _ = await opened(cog)
    i = interaction()
    await p.act(i, 'edit', key)
    m = i.response.send_modal.call_args.args[0]
    assert isinstance(m, SettingModal)
    m.input._value = raw
    await m.on_submit(interaction())
    assert getattr(p.config, key) == expected
    p.message.edit.assert_awaited_once()
    assert p.config.display(key) in text(p)

@pytest.mark.parametrize('key,raw', [('name', '   '), ('name', 'a\nb'), ('entry_fee', '-1'), ('entry_fee', '1.2'), ('entry_fee', 'abc'), ('entry_fee', '9223372036854775808')])
async def test_invalid_modal(cog, key, raw):
    p, _ = await opened(cog)
    m = SettingModal(p, key)
    m.input._value = raw
    i = interaction()
    await m.on_submit(i)
    assert p.config == DraftConfig()
    assert i.response.send_message.call_args.kwargs['ephemeral']
    p.message.edit.assert_not_awaited()

async def test_reset_cancel_timeout_and_stale_modal(cog):
    p, _ = await opened(cog)
    p.config = p.config.updated('name', 'Changed')
    await p.act(interaction(), 'reset')
    assert p.config.name == 'Changed' and p.reset_pending
    await p.act(interaction(), 'back')
    assert p.config.name == 'Changed'
    await p.act(interaction(), 'reset')
    await p.act(interaction(), 'reset_yes')
    assert p.config == DraftConfig()
    m = SettingModal(p, 'name')
    m.input._value = 'Too late'
    await p.act(interaction(), 'cancel')
    assert p.closed and not cog.setups
    assert not any(isinstance(x, (PanelButton, EditSelect)) for x in p.walk_children())
    await m.on_submit(interaction())
    assert p.config.name == DraftConfig().name
    p, _ = await opened(cog)
    await p.on_timeout()
    assert p.closed and not cog.setups
    assert 'expired' in text(p)

async def test_confirm_persistence_and_no_runner(cog):
    p, _ = await opened(cog)
    p.config = p.config.updated('slots', 64).updated('format', 'Double Elimination')
    cog._spawn = lambda _: pytest.fail('Runner must not start')
    i = interaction()
    await p.act(i, 'confirm')
    assert p.closed and not cog.setups and not cog.lobbies
    i.edit_original_response.assert_awaited_once()
    store = DraftStore(cog.draft_store.path)
    assert store.load(1) == (MASTER_ID, p.config)
    p2, _ = await opened(cog)
    assert p2.config == p.config
    p2.config = p2.config.updated('name', 'Unsaved')
    await p2.act(interaction(), 'cancel')
    assert store.load(1)[1].name == p.config.name

async def test_duplicate_and_send_failure(cog):
    p, _ = await opened(cog)
    send = AsyncMock()
    await cog.open_panel(send, NS(id=1), user(), NS(id=2))
    assert send.call_args.kwargs['ephemeral'] and cog.setups[1] is p
    await p.on_timeout()
    cog.lobbies[1] = NS(finished=False)
    await cog.open_panel(send, NS(id=1), user(), NS(id=2))
    assert not cog.setups
    cog.lobbies.clear()
    with pytest.raises(RuntimeError):
        await cog.open_panel(AsyncMock(side_effect=RuntimeError('send failed')), NS(id=1), user(), NS(id=2))
    assert not cog.setups

async def test_commands(cog):
    ctx = NS(guild=NS(id=1), author=user(), channel=NS(id=2), send=AsyncMock(return_value=NS(edit=AsyncMock())))
    await TournamentCog.tournament_prefix.callback(cog, ctx)
    assert isinstance(ctx.send.call_args.kwargs['view'], HostPanel)
    await cog.setups[1].on_timeout()
    i = interaction()
    i.guild, i.channel = NS(id=1), NS(id=2)
    i.original_response = AsyncMock(return_value=NS(edit=AsyncMock()))
    await TournamentCog.tournament_slash.callback(cog, i)
    assert isinstance(i.response.send_message.call_args.kwargs['view'], HostPanel)

async def test_storage_failure_leaves_editor_retryable(cog):
    p, _ = await opened(cog)
    cog.draft_store.save = lambda *args: (_ for _ in ()).throw(OSError('disk full'))
    with pytest.raises(OSError):
        await p.act(interaction(), 'confirm')
    assert not p.closed and cog.setups[1] is p

async def test_concurrent_open_and_unload(cog):
    sender = AsyncMock(return_value=NS(edit=AsyncMock()))
    await asyncio.gather(*(cog.open_panel(sender, NS(id=1), user(), NS(id=2)) for _ in range(2)))
    assert len(cog.setups) == 1
    assert sum('ephemeral' in c.kwargs for c in sender.call_args_list) == 1
    p = cog.setups[1]
    await cog.cog_unload()
    assert p.closed and not cog.setups

async def test_stale_and_invalid_enum(cog):
    p, _ = await opened(cog)
    await p.act(interaction(), 'edit', 'slots')
    i = interaction()
    await p.act(i, 'value', ('slots', 'not a number'))
    assert i.response.send_message.call_args.kwargs['ephemeral']
    assert p.config.slots == 16
    await p.act(interaction(), 'value', ('slots', '64'))
    i = interaction()
    await p.act(i, 'value', ('slots', '4'))
    assert p.config.slots == 64
    assert i.response.send_message.call_args.kwargs['ephemeral']

async def test_confirm_rechecks_active_tournament(cog):
    p, _ = await opened(cog)
    cog.lobbies[1] = NS(finished=False)
    i = interaction()
    await p.act(i, 'confirm')
    assert not p.closed and cog.draft_store.load(1) is None
    assert i.response.send_message.call_args.kwargs['ephemeral']

async def test_reset_and_cancel_buttons_dispatch(cog):
    p, _ = await opened(cog)
    reset = next(x for x in p.walk_children() if isinstance(x, PanelButton) and x.action == 'reset')
    await reset.callback(interaction())
    yes = next(x for x in p.walk_children() if isinstance(x, PanelButton) and x.action == 'reset_yes')
    await yes.callback(interaction())
    confirm = next(x for x in p.walk_children() if isinstance(x, PanelButton) and x.action == 'confirm')
    await confirm.callback(interaction())
    assert cog.draft_store.load(1)
    p, _ = await opened(cog)
    cancel = next(x for x in p.walk_children() if isinstance(x, PanelButton) and x.action == 'cancel')
    await cancel.callback(interaction())
    assert p.closed

async def test_admin_hooks_cannot_start_drafts(cog):
    channel = NS(id=2, guild=NS(id=1), send=AsyncMock(return_value=NS(edit=AsyncMock())))
    assert await cog.admin_announce(channel, user(), 'Optional announcement')
    assert channel.send.call_args.kwargs.keys() == {'view'}
    assert not cog.admin_start(1)
    assert await cog.admin_cancel(1)
    assert not cog.setups

async def test_error_is_private_and_controls_remain(cog):
    p, _ = await opened(cog)
    i = interaction()
    await p.on_error(i, RuntimeError('failure'), None)
    assert i.response.send_message.call_args.kwargs['ephemeral']
    assert not p.closed
    i.response.is_done = lambda: True
    await p.on_error(i, RuntimeError('failure'), None)
    assert i.followup.send.call_args.kwargs['ephemeral']

async def test_maximum_name_is_safely_rendered(cog):
    p, _ = await opened(cog)
    p.config = p.config.updated('name', '@everyone **Championship**')
    p.build()
    assert '@\u200beveryone' in text(p)
    assert '\\*\\*' in text(p)
