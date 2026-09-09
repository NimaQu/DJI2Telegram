import asyncio
import json
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from qdc507_gateway.apns import APNsService
from qdc507_gateway.api.app import create_app
from qdc507_gateway.calls.controller import ClientCallController
from qdc507_gateway.calls.core import CallBridgeError, CallCoordinator
from qdc507_gateway.calls.voip import VoIPPushService
from qdc507_gateway.config import Settings
from qdc507_gateway.events import EventBus
from qdc507_gateway.models import CallState, utc_now
from qdc507_gateway.security import hash_token
from qdc507_gateway.storage.database import Database
from qdc507_gateway.web.calls import AudioTicketStore, WebAudioSession


@pytest.fixture
def settings(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / 'key.p8'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    return Settings(apns_enabled=True, apns_key_path=path, apns_key_id='KEY123',
                    apns_team_id='TEAM123', apns_bundle_id='app.test', incoming_call_frontend='app')


def controller(database=None, record_sink=None):
    actions = []
    async def mark(*args):
        actions.append(args)
    async def save(record):
        if database:
            database.save_call(record)
        if record_sink:
            record_sink(record)
    control = ClientCallController(CallCoordinator(), lambda n: mark('dial', n), lambda: mark('answer'),
                                   lambda: mark('hangup'), lambda c: mark('audio', c), lambda: mark('stop'),
                                   record_sink=save)
    return control, actions


def test_combined_registration_partial_null_and_independent_versions(settings):
    db = Database(':memory:')
    push = APNsService(settings, db)
    device = str(uuid.uuid4())
    push.register(device, 'aa', 'bb')
    sms_version = db.connection.execute('SELECT version FROM push_devices').fetchone()[0]
    voip = db.voip_devices('sandbox', 'app.test')[0]
    push.register(device, voip_token='cc')
    assert db.connection.execute('SELECT version FROM push_devices').fetchone()[0] == sms_version
    db.invalidate_voip_device(voip, time.time() + 1)
    assert db.voip_devices('sandbox', 'app.test')[0]['device_token'] == 'cc'
    push.register(device, device_token=None)
    assert push.status()['active_devices'] == 0
    assert len(db.voip_devices('sandbox', 'app.test')) == 1
    push.register(device, 'dd')
    push.register(device, voip_token=None)
    assert push.status()['active_devices'] == 1
    assert not db.voip_devices('sandbox', 'app.test')
    push.register(device, voip_token='ee')
    db.delete_push_device(device)
    assert not db.registered_installation(device, 'sandbox', 'app.test')


def test_registration_api_voip_only_and_validation(settings):
    db = Database(':memory:')
    db.replace_token(hash_token('test'), 'now')
    push = APNsService(settings, db)
    client = TestClient(create_app(db, EventBus(), {'apns': push}))
    url = '/api/v1/push/devices/' + str(uuid.uuid4())
    headers = {'Authorization': 'Bearer test'}
    assert client.put(url, json={'voip_token': 'aa'}).status_code == 401
    assert client.put(url, headers=headers, json={'voip_token': 'AABB'}).status_code == 200
    assert db.voip_devices('sandbox', 'app.test')[0]['device_token'] == 'aabb'
    for payload in ({}, {'voip_token': 'a'}, {'voip_token': 'zz'}, {'voip_token': 42}, {'voip_token': 'aa' * 257}):
        assert client.put(url, headers=headers, json=payload).status_code == 422
    assert client.put(url, headers=headers, json={'device_token': 'cc'}).status_code == 200
    assert client.put(url, headers=headers, json={'voip_token': None}).status_code == 200
    assert push.status()['active_devices'] == 1
    assert not db.voip_devices('sandbox', 'app.test')


@pytest.mark.asyncio
async def test_app_answer_is_atomic_claim_before_audio_and_duplicate_socket_is_safe():
    control, actions = controller()
    call = await control.start_inbound('+123', frontend='app')
    outcomes = await asyncio.gather(control.answer(call.id, 'one'), control.answer(call.id, 'two'), return_exceptions=True)
    assert sum(isinstance(r, CallBridgeError) for r in outcomes) == 1
    assert call.owner_installation_id == 'one' and call.state == CallState.waiting_client
    assert not actions  # Neither microphone nor modem answer is required to claim.
    assert await control.answer(call.id, 'one') is call
    with pytest.raises(CallBridgeError):
        await control.require_owner(call.id, 'two')
    with pytest.raises(CallBridgeError):
        await control.hangup(call.id, installation_id='two', client=True)
    await control.reserve_audio(call.id, 'one')
    await control.attach_audio(call.id, 'one')
    assert call.state == CallState.active
    assert actions == [('audio', call.id), ('answer',)]
    with pytest.raises(CallBridgeError):
        await control.reserve_audio(call.id, 'one')
    assert await control.coordinator.current() is call
    await control.hangup(call.id, installation_id='one', client=True)


@pytest.mark.asyncio
async def test_app_websocket_ready_does_not_wait_for_microphone():
    control, actions = controller()
    call = await control.start_inbound('+123', frontend='app')
    await control.answer(call.id, 'one')
    session = WebAudioSession(control, SimpleNamespace())
    async def no_initial_pcm(_):
        pytest.fail('CallKit should not require PCM before readiness')
    async def stream(*args, **kwargs):
        assert call.state == CallState.active
        assert ('answer',) in actions
    session._receive_initial_audio = no_initial_pcm
    session.stream = stream
    await session.run(object(), call.id, 'one')
    assert call.state == CallState.ended


@pytest.mark.asyncio
async def test_call_expiry_and_history_with_owner_and_restart(tmp_path):
    db = Database(tmp_path / 'calls.sqlite3')
    control, _ = controller(db)
    call = await control.start_inbound('+123', frontend='app')
    await control.answer(call.id, 'one')
    assert db.get_call(call.id)['owner_installation_id'] == 'one'
    call.expires_at = utc_now() - timedelta(seconds=1)
    with pytest.raises(CallBridgeError, match='expired'):
        await control.attach_audio(call.id, 'one')
    db.recover_client_calls()
    assert db.get_call(call.id)['last_error'] == 'server restarted'
    assert db.get_call(call.id)['state'] == 'ended'
    await control.hangup(call.id)
    assert db.get_call(call.id)['ended_at'] is not None
    db.close()


def test_ticket_owner_is_preserved_and_one_use():
    tickets = AudioTicketStore()
    value = tickets.issue('call', 'one')
    assert tickets.take('call', value['ticket']) == {'installation_id': 'one'}
    assert tickets.take('call', value['ticket']) is None
    assert tickets.consume('web', tickets.issue('web')['ticket'])


@pytest.mark.asyncio
@pytest.mark.parametrize('sandbox', [True, False])
async def test_voip_headers_payload_and_duplicate_urcs(settings, sandbox):
    settings = replace(settings, apns_sandbox=sandbox)
    db = Database(':memory:')
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    push = APNsService(settings, db, client=client)
    push.register(str(uuid.uuid4()), 'aa', 'bb')
    control, _ = controller()
    voip = VoIPPushService(push, control.coordinator.current)
    call = await control.start_inbound('+123', frontend='app')
    voip.on_state(call)
    await voip.tasks[call.id]
    voip.on_state(call)
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == '/3/device/bb'
    assert request.url.host == ('api.sandbox.push.apple.com' if sandbox else 'api.push.apple.com')
    assert request.headers['apns-topic'] == 'app.test.voip'
    assert request.headers['apns-push-type'] == 'voip'
    assert request.headers['apns-expiration'] == '0'
    payload = json.loads(request.content)
    assert payload['call_id'] == call.id and payload['caller'] == '+123'
    assert payload['expires_at'] == call.expires_at.isoformat()
    assert push.status()['queued'] == 0
    await control.hangup(call.id)
    voip.on_state(call)
    await voip.stop()
    await client.aclose()


@pytest.mark.asyncio
async def test_voip_invalid_token_does_not_disable_sms_and_old_response_is_safe(settings):
    db = Database(':memory:')
    push = APNsService(settings, db)
    device = str(uuid.uuid4())
    push.register(device, 'aa', 'bb')
    control, _ = controller()
    call = await control.start_inbound('+123', frontend='app')
    voip = VoIPPushService(push, control.coordinator.current)
    old = db.voip_devices('sandbox', 'app.test')[0]
    async def handler(request):
        push.register(device, voip_token='cc')
        return httpx.Response(400, json={'reason': 'BadDeviceToken'})
    push.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await voip._send(call.id, old)
    assert db.voip_devices('sandbox', 'app.test')[0]['device_token'] == 'cc'
    assert push.status()['active_devices'] == 1
    await push.client.aclose()
    push.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, json={'reason': 'BadDeviceToken'})))
    await voip._send(call.id, db.voip_devices('sandbox', 'app.test')[0])
    assert not db.voip_devices('sandbox', 'app.test')
    assert push.status()['active_devices'] == 1
    await control.hangup(call.id)
    await push.stop()


@pytest.mark.asyncio
async def test_voip_end_cancels_inflight_and_no_late_invitation(settings):
    db = Database(':memory:')
    started = asyncio.Event()
    async def handler(request):
        started.set()
        await asyncio.Event().wait()
    push = APNsService(settings, db, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    push.register(str(uuid.uuid4()), voip_token='bb')
    control, _ = controller()
    voip = VoIPPushService(push, control.coordinator.current)
    call = await control.start_inbound('+123', frontend='app')
    voip.on_state(call)
    await started.wait()
    task = voip.tasks[call.id]
    await control.hangup(call.id)
    voip.on_state(call)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert await voip._ringing(call.id) is None
    await voip.stop()
    await push.stop()


@pytest.mark.asyncio
async def test_app_api_owner_guards_detail_and_outbound(settings, tmp_path):
    from qdc507_gateway.server import build_app
    settings = replace(settings, apns_enabled=False, data_dir=tmp_path / 'data')
    app = build_app(settings)
    db = app.state.gateway_database
    db.replace_token(hash_token('test'), 'now')
    one, two = str(uuid.uuid4()), str(uuid.uuid4())
    db.register_voip_device(one, 'aa', 'sandbox', 'app.test')
    db.register_voip_device(two, 'bb', 'sandbox', 'app.test')
    control = app.state.gateway_web_calls
    actions = []
    async def mark(*args):
        actions.append(args)
    control.audio_start = lambda c: mark('audio', c)
    control.audio_stop = lambda: mark('stop')
    control.cellular_answer = lambda: mark('answer')
    control.cellular_hangup = lambda: mark('hangup')
    control.cellular_dial = lambda n: mark('dial', n)
    call = await control.start_inbound('+123', frontend='app')
    headers = {'Authorization': 'Bearer test'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test', headers=headers) as client:
        url = '/api/v1/calls/' + call.id
        assert (await client.get(url)).json()['frontend'] == 'app'
        assert (await client.post(url + '/answer')).status_code == 409
        assert (await client.post(url + '/answer', json={'installation_id': one})).json()['state'] == 'waiting_client'
        assert (await client.post(url + '/answer', json={'installation_id': two})).status_code == 409
        assert (await client.post(url + '/audio-ticket', json={'installation_id': two})).status_code == 409
        ticket = await client.post(url + '/audio-ticket', json={'installation_id': one})
        assert ticket.status_code == 200 and ticket.json()['expires_in'] == 30
        assert (await client.post(url + '/hangup', json={'installation_id': two})).status_code == 409
        assert (await client.post(url + '/hangup', json={'installation_id': one})).status_code == 200
        detail = (await client.get(url)).json()
        assert detail['state'] == 'ended' and detail['owner_installation_id'] == one
        assert (await client.get('/api/v1/calls/missing')).status_code == 404
        result = await client.post('/api/v1/calls/start', json={'frontend': 'app', 'number': '+123', 'installation_id': one})
        assert result.status_code == 200 and result.json()['owner_installation_id'] == one
        assert not any(action[0] == 'dial' for action in actions)
        await client.post('/api/v1/calls/' + result.json()['id'] + '/hangup', json={'installation_id': one})
    app.state.gateway_module_service.close()
    await app.state.gateway_runtime.stop()
    db.close()


@pytest.mark.asyncio
async def test_app_routing_never_notifies_telegram(settings, tmp_path, monkeypatch):
    from qdc507_gateway import server
    async def noop(*args, **kwargs):
        return None
    async def forbidden(*args, **kwargs):
        pytest.fail('App incoming route touched Telegram')
    monkeypatch.setattr(server.KurigramTelegramService, 'start', noop)
    monkeypatch.setattr(server.KurigramTelegramService, 'stop', noop)
    monkeypatch.setattr(server.KurigramTelegramService, 'notify_incoming_cellular_call', forbidden)
    monkeypatch.setattr(server.KurigramTelegramService, 'request_private_call', forbidden)
    app = server.build_app(replace(settings, apns_enabled=False, data_dir=tmp_path / 'data'))
    handlers = {}
    async def start_monitor(**kwargs):
        handlers.update(kwargs)
    module = app.state.gateway_module_service
    monkeypatch.setattr(module, 'start_monitor', start_monitor)
    monkeypatch.setattr(module, 'stop_monitor', noop)
    monkeypatch.setattr(app.state.gateway_runtime, 'probe_once', noop)
    async def signal():
        return {}
    monkeypatch.setattr(module, 'signal', signal)
    monkeypatch.setattr(module, 'network_status', signal)
    monkeypatch.setattr(app.state.gateway_web_calls, 'cellular_hangup', noop)
    async with app.router.lifespan_context(app):
        record = await handlers['on_incoming_call']('+123')
        assert record.frontend == 'app'
        await handlers['on_call_disconnected']()
        assert record.state == CallState.ended


@pytest.mark.asyncio
async def test_voip_retry_checks_ring_state_and_token_before_each_attempt(settings, monkeypatch):
    db = Database(':memory:')
    push = APNsService(settings, db)
    push.register('one', voip_token='aa')
    control, _ = controller()
    call = await control.start_inbound('+123', frontend='app')
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(503)
    push.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    voip = VoIPPushService(push, control.coordinator.current)
    async def end_during_backoff(_):
        await control.answer(call.id, 'one')
    monkeypatch.setattr('qdc507_gateway.calls.voip.asyncio.sleep', end_during_backoff)
    await voip._send(call.id, db.voip_devices('sandbox', 'app.test')[0])
    assert len(requests) == 1
    await control.hangup(call.id)
    await push.stop()
