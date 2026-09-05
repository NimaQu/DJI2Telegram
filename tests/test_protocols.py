import struct
import asyncio
import time

from qdc507_gateway.adb.protocol import (
    ADBFrame,
    CLSE,
    CNXN,
    OKAY,
    SYNC_DATA,
    SYNC_DONE,
    SYNC_OKAY,
    SYNC_SEND,
    WRTE,
    decode_frame,
    encode_frame,
)
from qdc507_gateway.adb.transport import ADBClient, ADBTransport
from qdc507_gateway.audio.alsa import find_qdc507_pcm_devices, resample_pcm16_mono
from qdc507_gateway.audio.bridge import AlsaNTgCallsAudioAdapter, PCMBridge
from qdc507_gateway.audio.alsa import ALSAUnavailable
from qdc507_gateway.audio.ring import PCMFrame, RingBuffer
from qdc507_gateway.security import dangerous_at_command, hash_token, verify_token


def test_adb_frame_round_trip():
    frame = ADBFrame(CNXN, 1, 2, b"host::qdc507\0")
    assert decode_frame(encode_frame(frame)) == frame


def test_adb_client_cnxn_shell_and_sync_pull():
    class FakeTransport:
        def __init__(self, received):
            self.received = list(received)
            self.sent = []

        def open(self):
            pass

        def close(self):
            pass

        def send(self, frame):
            self.sent.append(frame)

        def receive(self, timeout_ms=2000):
            return self.received.pop(0)

    transport = FakeTransport([
        ADBFrame(CNXN, 1, 4096),
        ADBFrame(OKAY, 9, 2),
        ADBFrame(WRTE, 9, 2, b"uid=0\n"),
        ADBFrame(CLSE, 9, 2),
        ADBFrame(OKAY, 9, 2),
        ADBFrame(OKAY, 10, 3),
        ADBFrame(OKAY, 10, 3),
        ADBFrame(WRTE, 10, 3, struct.pack("<II", SYNC_DATA, 3)),
        ADBFrame(WRTE, 10, 3, b"abc"),
        ADBFrame(WRTE, 10, 3, struct.pack("<II", SYNC_DONE, 0)),
    ])
    client = ADBClient(transport)
    client.connect()
    assert client.shell("id") == "uid=0\n"
    assert client.pull("/data/local/tmp/x") == b"abc"
    assert transport.sent[0].command == CNXN
    assert any(frame.command == OKAY for frame in transport.sent)


def test_adb_transport_sends_header_and_payload_as_separate_writes():
    writes = []
    transport = ADBTransport(writes.append, lambda _length, _timeout: b"")
    transport.open()
    frame = ADBFrame(CNXN, 0x01000001, 4096, b"host::features=shell_v2\0")
    transport.send(frame)
    assert len(writes) == 2
    assert len(writes[0]) == 24
    assert writes[1] == frame.payload


def test_adb_client_sync_push():
    class FakeTransport:
        def __init__(self, received):
            self.received = list(received)
            self.sent = []

        def open(self):
            pass

        def close(self):
            pass

        def send(self, frame):
            self.sent.append(frame)

        def receive(self, timeout_ms=2000):
            return self.received.pop(0)

    transport = FakeTransport([
        ADBFrame(CNXN, 1, 4096),
        ADBFrame(OKAY, 9, 2),
        ADBFrame(OKAY, 9, 2),
        ADBFrame(OKAY, 9, 2),
        ADBFrame(OKAY, 9, 2),
        ADBFrame(WRTE, 9, 2, struct.pack("<II", SYNC_OKAY, 0)),
        ADBFrame(CLSE, 9, 2),
    ])
    client = ADBClient(transport)
    client.connect()
    client.push(b"abc", "/data/local/tmp/x", 0o700)
    payloads = [frame.payload for frame in transport.sent if frame.command == WRTE]
    assert any(payload[:4] == struct.pack("<I", SYNC_SEND) for payload in payloads)
    assert any(payload[:4] == struct.pack("<I", SYNC_DATA) for payload in payloads)
    assert any(payload[:4] == struct.pack("<I", SYNC_DONE) for payload in payloads)


def test_adb_sync_data_frames_stay_within_negotiated_payload():
    class FakeTransport:
        def __init__(self, received):
            self.received = list(received)
            self.sent = []

        def open(self):
            pass

        def close(self):
            pass

        def send(self, frame):
            self.sent.append(frame)

        def receive(self, timeout_ms=2000):
            return self.received.pop(0)

    remote_id = 9
    local_id = 2
    transport = FakeTransport([
        ADBFrame(CNXN, 1, 32),
        ADBFrame(OKAY, remote_id, local_id),
        ADBFrame(OKAY, remote_id, local_id),
        ADBFrame(OKAY, remote_id, local_id),
        ADBFrame(OKAY, remote_id, local_id),
        ADBFrame(OKAY, remote_id, local_id),
        ADBFrame(OKAY, remote_id, local_id),
        ADBFrame(WRTE, remote_id, local_id, struct.pack("<II", SYNC_OKAY, 0)),
    ])
    client = ADBClient(transport)
    client.connect()
    client.push(b"x" * 50, "/x", 0o700)
    writes = [frame.payload for frame in transport.sent if frame.command == WRTE]
    assert all(len(payload) <= 32 for payload in writes)
    data_packets = [payload for payload in writes if payload[:4] == struct.pack("<I", SYNC_DATA)]
    assert [len(payload) for payload in data_packets] == [32, 32, 10]


def test_ring_buffer_is_bounded_and_drops_oldest():
    ring = RingBuffer(2)
    ring.put(PCMFrame(b"1"))
    ring.put(PCMFrame(b"2"))
    ring.put(PCMFrame(b"3"))
    assert ring.dropped == 1
    assert ring.get().data == b"2"
    assert ring.get().data == b"3"


def test_pcm_bridge_is_bounded_and_requires_active_call():
    bridge = PCMBridge(capacity=1)
    frame = PCMFrame(b"\x01\x00")
    assert not bridge.push_cellular(frame)
    asyncio_bridge = bridge

    async def run():
        await asyncio_bridge.start()
        assert asyncio_bridge.push_cellular(frame)
        assert asyncio_bridge.pull_for_telegram() == frame
        await asyncio_bridge.stop()
        assert asyncio_bridge.pull_for_cellular() is None

    import asyncio
    asyncio.run(run())


def test_pcm_bridge_records_xruns_per_direction():
    bridge = PCMBridge(capacity=1)
    bridge.record_xrun("cellular_to_telegram")
    bridge.record_xrun("telegram_to_cellular")
    stats = bridge.stats()
    assert stats["cellular_to_telegram"]["xruns"] == 1
    assert stats["telegram_to_cellular"]["xruns"] == 1


def test_pcm_bridge_telegram_cue_is_local_and_click_faded():
    bridge = PCMBridge(capacity=2)

    async def run():
        silence = b"\0" * 160
        await bridge.start()
        assert bridge.queue_telegram_cue(duration_ms=20)
        first = bridge.mix_telegram_cue(silence, 8000)
        second = bridge.mix_telegram_cue(silence, 8000)
        assert first != silence
        assert second != silence
        assert bridge.mix_telegram_cue(silence, 8000) == silence
        assert bridge.telegram_to_cellular.stats()["frames_in"] == 0
        await bridge.stop()
        assert not bridge.queue_telegram_cue()

    asyncio.run(run())


def test_audio_stats_and_pcm16_resampling(tmp_path):
    ring = RingBuffer(2)
    ring.put(PCMFrame(b"\x01\x00\x00\x00"))
    ring.record_xrun()
    assert ring.get() is not None
    assert ring.stats()["nonzero_samples"] == 1
    assert ring.stats()["xruns"] == 1
    assert ring.stats()["first_frame_ms"] is not None
    assert ring.stats()["first_nonzero_ms"] is not None

    converted = resample_pcm16_mono(b"\x00\x00\xff\x7f", 8000, 16000)
    assert len(converted) == 8

    usb = tmp_path / "devices" / "usb" / "2-1"
    card = usb / "sound" / "card9"
    card.mkdir(parents=True)
    (usb / "idVendor").write_text("2c7c\n", encoding="ascii")
    (usb / "idProduct").write_text("0125\n", encoding="ascii")
    (card / "pcmC9D0c").mkdir()
    (card / "pcmC9D0p").mkdir()
    sound = tmp_path / "class" / "sound"
    sound.mkdir(parents=True)
    (sound / "card9").symlink_to(card)
    endpoints = find_qdc507_pcm_devices(tmp_path)
    assert {item.name for item in endpoints} == {"hw:9,0"}
    assert {item.direction for item in endpoints} == {"capture", "playback"}

    (usb / "idVendor").write_text("2ca3\n", encoding="ascii")
    (usb / "idProduct").write_text("4006\n", encoding="ascii")
    assert find_qdc507_pcm_devices(tmp_path) == ()
    configured = find_qdc507_pcm_devices(tmp_path, 0x2CA3, 0x4006)
    assert configured == endpoints


def test_audio_adapter_reports_missing_uac_without_claiming_devices(tmp_path):
    async def run():
        events = []

        async def publish(event):
            events.append(event)

        adapter = AlsaNTgCallsAudioAdapter(
            lambda: object(),
            sysfs_root=tmp_path,
            event_publisher=publish,
        )
        try:
            await adapter.start(42)
        except ALSAUnavailable:
            pass
        else:
            raise AssertionError("missing UAC endpoints were accepted")
        assert [event.type for event in events] == ["audio.error"]

    import asyncio
    asyncio.run(run())


def test_audio_adapter_fills_idle_playback_periods_with_silence():
    class FakeAlsa:
        def __init__(self):
            self.silence_periods = 0

        def write_silence(self):
            self.silence_periods += 1
            time.sleep(0.002)

        def write(self, _frame):
            raise AssertionError("no real frame was queued")

    async def run():
        adapter = AlsaNTgCallsAudioAdapter(lambda: None)
        fake = FakeAlsa()
        adapter.alsa = fake
        adapter._stop.clear()
        task = asyncio.create_task(adapter._playback_loop())
        await asyncio.sleep(0.015)
        adapter._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert fake.silence_periods > 0

    asyncio.run(run())


def test_audio_adapter_does_not_reuse_uncertain_module_voice_cleanup():
    class Runtime:
        def __init__(self):
            self.stop_calls = 0

        async def stop_async(self):
            self.stop_calls += 1
            raise RuntimeError("cleanup failed")

    async def run():
        runtime = Runtime()
        adapter = AlsaNTgCallsAudioAdapter(
            lambda: None,
            module_runtime=runtime,
        )
        adapter._module_runtime_started = True

        with pytest.raises(RuntimeError, match="cleanup failed"):
            await adapter.stop()
        assert adapter._module_runtime_started is True

        with pytest.raises(ALSAUnavailable, match="cleanup was not confirmed"):
            await adapter.start_web("next-call")
        assert runtime.stop_calls == 1

    import pytest
    asyncio.run(run())


def test_token_hash_and_dangerous_command_policy():
    token = "test-token"
    encoded = hash_token(token)
    assert verify_token(token, encoded)
    assert not verify_token("wrong", encoded)
    assert dangerous_at_command('AT+QCFG="USBCFG",0x2C7C,0x0125,1,1,1,1,1,1,1')
    assert not dangerous_at_command("AT+CSQ")
