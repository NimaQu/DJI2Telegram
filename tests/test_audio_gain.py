import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qdc507_gateway.audio.alsa import scale_pcm16
from qdc507_gateway.audio.ring import PCMFrame
from qdc507_gateway.config import ConfigurationError, Settings
from qdc507_gateway.web.calls import WebAudioSession


def test_gain_config_and_exact_samples(tmp_path):
    path = tmp_path / 'config.toml'
    for value in ('-0.1', '1.1', 'nan', 'inf', 'true', '"0.9"'):
        path.write_text('[calls]\naudio_gain = ' + value)
        with pytest.raises(ConfigurationError):
            Settings.load(path)
    path.write_text('[calls]\naudio_gain = 0.9')
    assert Settings.load(path).audio_gain == .9
    path.write_text('')
    assert Settings.load(path).audio_gain == 1
    data = struct.pack('<5h', -32768, -10000, 0, 10000, 32767)
    assert scale_pcm16(data, 1) is data
    assert scale_pcm16(data, 0) == bytes(10)
    assert struct.unpack('<5h', scale_pcm16(data, .9)) == (-29491, -9000, 0, 9000, 29490)


@pytest.mark.asyncio
async def test_both_ws_directions_scaled_once():
    data = struct.pack('<h', 10000) * 160
    received = []
    pcm = SimpleNamespace(push_client=lambda frame: received.append(frame),
                          pull_for_client=lambda: PCMFrame(data))
    session = WebAudioSession(None, SimpleNamespace(pcm_bridge=pcm), audio_gain=.9)
    messages = iter([{'type': 'websocket.receive', 'bytes': data}, {'type': 'websocket.disconnect'}])
    class Finished(Exception):
        pass
    class Socket:
        async def receive(self):
            return next(messages)
        async def send_bytes(self, output):
            assert output == struct.pack('<h', 9000) * 160
            raise Finished
    await session._receive_audio(Socket())
    assert received[0].data == struct.pack('<h', 9000) * 160
    with pytest.raises(Finished):
        await session._send_audio(Socket())


@pytest.mark.asyncio
async def test_browser_initial_frames_scaled():
    frames = []
    controller = SimpleNamespace(reserve_audio=AsyncMock(), attach_audio=AsyncMock(),
                                 websocket_disconnected=AsyncMock())
    session = WebAudioSession(controller, SimpleNamespace(pcm_bridge=SimpleNamespace(push_client=frames.append)), audio_gain=.5)
    session._receive_initial_audio = AsyncMock(return_value=[PCMFrame(struct.pack('<h', -10000) * 160)])
    session.stream = AsyncMock()
    await session.run(None, 'call')
    assert frames[0].data == struct.pack('<h', -5000) * 160
