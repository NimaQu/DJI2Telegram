import asyncio
import errno
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from qdc507_gateway.audio.alsa import AlsaPCMDevice, ALSAUnavailable
from qdc507_gateway.audio.bridge import AlsaAudioAdapter
from qdc507_gateway.audio.jitter import PlaybackJitterBuffer
from qdc507_gateway.audio.ring import PCMFrame, RingBuffer


def frame(value):
    return PCMFrame(struct.pack("<h", value) * 160)


def test_playback_prefills_and_adapts_after_starvation_without_losing_samples():
    queue = PlaybackJitterBuffer()
    queue.put(frame(1))
    queue.put(frame(2))
    assert queue.get() is None
    queue.put(frame(3))
    assert [queue.get().data for _ in range(3)] == [frame(n).data for n in range(1, 4)]
    assert queue.get() is None
    assert queue.stats()["underruns"] == 1
    assert queue.stats()["target_ms"] == 80
    for n in range(4, 7):
        queue.put(frame(n))
    assert queue.get() is None
    queue.put(frame(7))
    assert [queue.get().data for _ in range(4)] == [frame(n).data for n in range(4, 8)]
    assert queue.stats()["dropped"] == 0
    queue.reset()
    assert queue.stats()["target_ms"] == 60
    assert queue.stats()["underruns"] == 0


def test_jitter_reduces_holes_under_batched_variable_network_delivery():
    # Maximum legal 200 ms packets arrive at alternating 260/140 ms intervals.
    arrivals = {}
    for packet in range(100):
        due = packet * 200 + (60 if packet % 2 else 0)
        arrivals[due] = [frame(packet * 10 + n) for n in range(10)]

    def simulate(queue):
        holes = 0
        started = False
        for tick in range(0, 19000, 20):
            for data in arrivals.get(tick, []):
                queue.put(data)
            data = queue.get()
            if data is None and started:
                holes += 1
            if data is not None:
                started = True
        return holes

    raw = RingBuffer(10)
    buffered = PlaybackJitterBuffer()
    assert simulate(buffered) < simulate(raw) / 4
    assert buffered.stats()["dropped"] == 0
    assert buffered.stats()["target_ms"] <= 120


def test_playback_capacity_remains_bounded():
    queue = PlaybackJitterBuffer()
    for n in range(100):
        queue.put(frame(n))
    assert len(queue) == 20
    assert queue.stats()["dropped"] == 80
    assert queue.get().data == frame(80).data


def device(monkeypatch, results):
    monkeypatch.setitem(sys.modules, "alsaaudio", SimpleNamespace())
    output = AlsaPCMDevice("fake")
    submitted = []
    replies = iter(results)

    def write(data):
        submitted.append(data)
        return next(replies)

    output.playback = SimpleNamespace(write=write)
    return output, submitted


def test_alsa_retries_same_bytes_after_underrun_and_handles_partial_write(monkeypatch):
    output, submitted = device(monkeypatch, [-errno.EPIPE, 80, 80])
    data = frame(100)
    output.write(data)
    assert submitted == [data.data, data.data, data.data[160:]]
    assert output.frames_written == 160
    assert output.playback_recoveries == 1
    assert output.partial_writes == 1
    assert output.write_failures == 0


def test_repeated_alsa_failure_is_bounded_and_not_counted_as_written(monkeypatch):
    output, submitted = device(monkeypatch, [-errno.EPIPE] * 4)
    with pytest.raises(ALSAUnavailable):
        output.write(frame(1))
    assert len(submitted) == 4
    assert output.frames_written == 0
    assert output.write_failures == 1


def test_zero_write_retries_without_losing_data(monkeypatch):
    output, submitted = device(monkeypatch, [0, 160])
    output.write(frame(2))
    assert submitted[0] == submitted[1]
    assert output.frames_written == 160


@pytest.mark.asyncio
async def test_playback_worker_continues_while_event_loop_is_busy():
    adapter = AlsaAudioAdapter()
    writes = []

    def write_silence():
        writes.append(threading.get_ident())
        time.sleep(0.002)

    adapter.alsa = SimpleNamespace(write_silence=write_silence)
    worker = threading.Thread(target=adapter._playback_worker)
    worker.start()
    try:
        # Deliberately block the asyncio loop: ALSA is driven by its own thread.
        time.sleep(0.04)
        assert len(writes) >= 5
        assert all(ident != threading.get_ident() for ident in writes)
    finally:
        adapter._stop.set()
        await asyncio.to_thread(worker.join, 1)
        assert not worker.is_alive()


@pytest.mark.asyncio
async def test_shutdown_joins_worker_before_closing_alsa():
    adapter = AlsaAudioAdapter()
    entered, release = threading.Event(), threading.Event()
    order = []

    def read():
        entered.set()
        release.wait(1)
        order.append("read finished")
        return frame(0)

    def close():
        order.append("closed")

    adapter.alsa = SimpleNamespace(read=read, close=close, stats=lambda: {})
    worker = threading.Thread(target=adapter._capture_worker)
    adapter._workers = [worker]
    worker.start()
    await asyncio.to_thread(entered.wait, 1)
    stop = asyncio.create_task(adapter.stop())
    await asyncio.sleep(0.01)
    assert "closed" not in order
    release.set()
    await stop
    assert order == ["read finished", "closed"]
