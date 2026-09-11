import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from qdc507_gateway.api.app import create_app
from qdc507_gateway.calls.controller import ClientCallController
from qdc507_gateway.calls.core import CallBridgeError, CallCoordinator
from qdc507_gateway.events import EventBus
from qdc507_gateway.modem.service import LiveModuleService, ModuleServiceError
from qdc507_gateway.security import hash_token
from qdc507_gateway.storage.database import Database


async def active_call():
    noop = AsyncMock()
    control = ClientCallController(CallCoordinator(), noop, noop, noop, noop, noop,
                                   cellular_dtmf=AsyncMock())
    call = await control.start_inbound('+123', frontend='app')
    await control.answer(call.id, 'owner')
    await control.attach_audio(call.id, 'owner')
    return control, call


@pytest.mark.asyncio
async def test_owner_state_and_stale_call():
    control, call = await active_call()
    for call_id, owner in ((call.id, 'other'), ('old-call', 'owner')):
        with pytest.raises(CallBridgeError):
            await control.send_dtmf(call_id, owner, '1')
    assert await control.send_dtmf(call.id, 'owner', '#') == {'call_id': call.id, 'accepted': True}
    control.cellular_dtmf.assert_awaited_once_with('#')
    await control.hangup(call.id)
    with pytest.raises(CallBridgeError):
        await control.send_dtmf(call.id, 'owner', '1')
    call = await control.start_inbound('+123', frontend='app')
    await control.answer(call.id, 'owner')
    with pytest.raises(CallBridgeError):
        await control.send_dtmf(call.id, 'owner', '1')
    await control.hangup(call.id)


@pytest.mark.asyncio
async def test_cancelled_request_keeps_hangup_serialized():
    control, call = await active_call()
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(digits):
        entered.set()
        await release.wait()
    control.cellular_dtmf = blocked
    send = asyncio.create_task(control.send_dtmf(call.id, 'owner', '1'))
    await entered.wait()
    send.cancel()
    hangup = asyncio.create_task(control.hangup(call.id))
    await asyncio.sleep(0)
    assert not hangup.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await send
    await hangup
    with pytest.raises(CallBridgeError):
        await control.send_dtmf(call.id, 'owner', '2')


@pytest.mark.asyncio
async def test_modem_command_and_errors():
    service = LiveModuleService(Database(':memory:'), EventBus())
    service.at = AsyncMock(return_value={'ok': True})
    assert await service.send_dtmf('*') == {'accepted': True}
    service.at.assert_awaited_once_with('AT+VTS="*",1', timeout_ms=3000)
    for digits in ('', '12', '";ATH', '\n', 'a'):
        with pytest.raises(ModuleServiceError):
            await service.send_dtmf(digits)
    service.at.return_value = {'ok': False}
    with pytest.raises(ModuleServiceError, match='rejected'):
        await service.send_dtmf('1')


def test_api_auth_validation_and_errors():
    db = Database(':memory:')
    db.replace_token(hash_token('test'), 'now')
    handler = AsyncMock(return_value={'call_id': 'call', 'accepted': True})
    client = TestClient(create_app(db, EventBus(), {'send_dtmf': handler}))
    url = '/api/v1/calls/call/dtmf'
    payload = {'installation_id': str(uuid.uuid4()), 'digits': '1'}
    headers = {'Authorization': 'Bearer test'}
    assert client.post(url, json=payload).status_code == 401
    for invalid in ({}, {**payload, 'digits': '12'}, {**payload, 'installation_id': 'bad'},
                    {**payload, 'digits': '\n'}, {**payload, 'digits': 1}):
        assert client.post(url, json=invalid, headers=headers).status_code == 422
    handler.assert_not_called()
    assert client.post(url, json=payload, headers=headers).json()['accepted'] is True
    handler.assert_awaited_once_with('call', payload['installation_id'], '1')
    handler.side_effect = CallBridgeError('call is not owned by this installation')
    assert client.post(url, json=payload, headers=headers).status_code == 409
    handler.side_effect = ModuleServiceError('sensitive raw command')
    result = client.post(url, json=payload, headers=headers)
    assert result.status_code == 502 and 'sensitive' not in result.text
