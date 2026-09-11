import io
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from qdc507_gateway.audio.recording import DebugRecordings
from qdc507_gateway.api.app import create_app
from qdc507_gateway.events import EventBus
from qdc507_gateway.security import hash_token
from qdc507_gateway.storage.database import Database
from qdc507_gateway.web.calls import WebAudioSession


def test_exact_pcm_wav_limits_and_retention():
    recordings = DebugRecordings(enabled=True)
    recordings.LIMIT = 320
    recordings.start('one')
    pcm = b'\xff\x7f\x00\x80' * 80
    recordings.append('one', 'client_to_bridge', pcm * 2)
    assert recordings.status('one')['truncated']
    assert not recordings.status('one')['active']
    with wave.open(io.BytesIO(recordings.wav(recordings.snapshot('one', 'client_to_bridge')))) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (8000, 1, 2)
        assert wav.readframes(160) == pcm
    assert recordings.snapshot('one', 'bridge_to_client') == b''
    for name in ('two', 'three', 'four'):
        recordings.start(name)
    assert 'one' not in recordings.records
    recordings.stop('four')
    recordings.append('four', 'client_to_bridge', pcm)
    assert recordings.snapshot('four', 'client_to_bridge') == b''


@pytest.mark.asyncio
async def test_ws_records_input_before_queue_acceptance():
    session = WebAudioSession(None, SimpleNamespace(pcm_bridge=SimpleNamespace(push_client=lambda frame: False)))
    pcm = b'\xff\x7f' * 160
    messages = iter([{'type': 'websocket.receive', 'bytes': pcm}, {'type': 'websocket.disconnect'}])
    class Socket:
        async def receive(self):
            return next(messages)
    session.recordings.enabled = True
    session.recordings.start('call')
    await session._receive_audio(Socket(), 'call')
    assert session.recordings.snapshot('call', 'client_to_bridge') == pcm


def test_recording_api_auth_download_and_delete():
    db = Database(':memory:')
    db.replace_token(hash_token('test'), 'now')
    recordings = DebugRecordings(enabled=True)
    recordings.start('call')
    recordings.append('call', 'bridge_to_client', b'\x01\x00' * 160)
    client = TestClient(create_app(db, EventBus(), {'recordings': recordings}))
    headers = {'Authorization': 'Bearer test'}
    url = '/api/v1/calls/call/recording'
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers).json()['active']
    result = client.get(url + '/bridge_to_client.wav', headers=headers)
    assert result.content[:4] == b'RIFF'
    assert client.get(url + '/invalid.wav', headers=headers).status_code == 422
    assert client.get('/api/v1/audio/recordings', headers=headers).json()['enabled']
    assert client.delete(url, headers=headers).status_code == 204
    assert client.get(url, headers=headers).status_code == 404


def test_config_switch_and_disabled_recording(tmp_path):
    from qdc507_gateway.config import ConfigurationError, Settings
    path = tmp_path / 'config.toml'
    path.write_text('[calls]\ndebug_recording_enabled = true\n')
    assert Settings.load(path).debug_recording_enabled
    path.write_text('[calls]\n')
    assert not Settings.load(path).debug_recording_enabled
    path.write_text('[calls]\ndebug_recording_enabled = 12\n')
    with pytest.raises(ConfigurationError):
        Settings.load(path)
    recordings = DebugRecordings()
    recordings.start('disabled')
    recordings.append('disabled', 'client_to_bridge', b'\0\0')
    assert not recordings.records


@pytest.mark.asyncio
async def test_auto_start_stop_and_output_bytes():
    from unittest.mock import AsyncMock
    from qdc507_gateway.audio.ring import PCMFrame
    controller = SimpleNamespace(reserve_audio=AsyncMock(), attach_audio=AsyncMock(),
                                 websocket_disconnected=AsyncMock())
    pcm = b'\x00\x80\xff\x7f' * 80
    bridge = SimpleNamespace(pull_for_client=lambda: PCMFrame(pcm))
    session = WebAudioSession(controller, SimpleNamespace(pcm_bridge=bridge), debug_recording_enabled=True)
    class Finished(Exception):
        pass
    class Socket:
        async def send_bytes(self, data):
            assert data == pcm
            if session.frames_to_browser:
                raise Finished
    async def stream(socket, call_id, **kwargs):
        assert session.recordings.status(call_id)['active']
        await session._send_audio(socket, call_id)
    session.stream = stream
    with pytest.raises(Finished):
        await session.run(Socket(), 'call', 'owner')
    assert not session.recordings.status('call')['active']
    assert session.recordings.snapshot('call', 'bridge_to_client') == pcm
    controller.websocket_disconnected.assert_awaited_once_with('call')
