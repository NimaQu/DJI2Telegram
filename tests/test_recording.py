import io
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from qdc507_gateway.audio.recording import DebugRecordings
from qdc507_gateway.api.app import create_app
from qdc507_gateway.events import EventBus
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


def test_recording_apis_removed():
    client = TestClient(create_app(Database(':memory:'), EventBus()))
    assert not any('recording' in path for path in client.get('/openapi.json').json()['paths'])


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
async def test_auto_start_stop_and_output_bytes(tmp_path):
    from unittest.mock import AsyncMock
    from qdc507_gateway.audio.ring import PCMFrame
    controller = SimpleNamespace(reserve_audio=AsyncMock(), attach_audio=AsyncMock(),
                                 websocket_disconnected=AsyncMock())
    pcm = b'\x00\x80\xff\x7f' * 80
    bridge = SimpleNamespace(pull_for_client=lambda: PCMFrame(pcm))
    session = WebAudioSession(controller, SimpleNamespace(pcm_bridge=bridge), debug_recording_enabled=True, recording_directory=tmp_path)
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
    assert not session.recordings.records
    files = list(tmp_path.glob('*/bridge_to_client.wav'))
    assert len(files) == 1
    with wave.open(str(files[0])) as wav:
        assert wav.readframes(160) == pcm
    controller.websocket_disconnected.assert_awaited_once_with('call')


@pytest.mark.asyncio
async def test_storage_failure_does_not_break_cleanup(tmp_path, caplog):
    blocked = tmp_path / 'file'
    blocked.write_text('not a directory')
    recordings = DebugRecordings(True, blocked)
    recordings.start('call')
    await recordings.finish('call')
    assert not recordings.records
    assert 'debug recording save failed' in caplog.text
