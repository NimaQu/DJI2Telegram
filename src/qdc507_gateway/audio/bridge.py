from __future__ import annotations

import asyncio
import inspect
import time
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .alsa import AlsaPCMDevice, ALSAUnavailable, find_qdc507_pcm_devices
from .ring import PCMFrame, RingBuffer
from .jitter import PlaybackJitterBuffer
from qdc507_gateway.models import GatewayEvent


class PCMBridge:
    """Bounded PCM boundary between the ALSA and client media loops.

    The media engines own their I/O callbacks; this class owns only bounded
    buffering and accounting. It never blocks indefinitely and never grows
    memory when one leg stops consuming frames.
    """

    def __init__(self, capacity: int = 50, playback_capacity: int | None = None):
        self.cellular_to_client = RingBuffer(capacity)
        playback_capacity = playback_capacity or capacity
        self.client_to_cellular = PlaybackJitterBuffer(
            playback_capacity, min(3, playback_capacity), min(6, playback_capacity),
        )
        self.running = False

    async def start(self, _call_id=None) -> None:
        self.cellular_to_client.reset()
        self.client_to_cellular.reset()
        self.running = True

    async def stop(self) -> None:
        self.running = False
        self.cellular_to_client.clear()
        self.client_to_cellular.clear()

    def push_cellular(self, frame: PCMFrame) -> bool:
        if not self.running:
            return False
        self.cellular_to_client.put(frame)
        return True

    def push_client(self, frame: PCMFrame) -> bool:
        if not self.running:
            return False
        self.client_to_cellular.put(frame)
        return True

    def pull_for_client(self) -> Optional[PCMFrame]:
        return self.cellular_to_client.get()

    def pull_for_cellular(self) -> Optional[PCMFrame]:
        return self.client_to_cellular.get()

    def stats(self) -> dict[str, object]:
        return {
            "running": self.running,
            "cellular_to_client": self.cellular_to_client.stats(),
            "client_to_cellular": self.client_to_cellular.stats(),
        }

    def record_xrun(self, direction: str = "cellular_to_client") -> None:
        """Record an overrun/underrun on the named bounded audio leg."""
        if direction == "cellular_to_client":
            self.cellular_to_client.record_xrun()
        elif direction == "client_to_cellular":
            self.client_to_cellular.record_xrun()
        else:
            raise ValueError("unknown PCM direction")


class AlsaAudioAdapter:
    """Connect QDC507 UAC/ALSA frames to a client PCM stream."""

    def __init__(
        self,
        sysfs_root: str | Path = "/sys",
        event_publisher: Optional[Callable[[GatewayEvent], Awaitable[Any]]] = None,
        module_runtime: Any = None,
    ):
        self.sysfs_root = sysfs_root
        # Capture stays low latency. Playback starts with 60 ms of reserve,
        # grows to 120 ms after starvation and tolerates a 200 ms packet burst.
        self.pcm_bridge = PCMBridge(capacity=10, playback_capacity=20)
        self.alsa: Optional[AlsaPCMDevice] = None
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._event_publisher = event_publisher
        self.module_runtime = module_runtime
        self._module_runtime_started = False
        self._mode: Optional[str] = None
        self._lifecycle_lock = asyncio.Lock()
        self._session_started_at: Optional[float] = None
        self._last_session: Optional[dict[str, object]] = None

    async def start_web(self, _call_id: str) -> None:
        """Open QDC507 UAC for a client audio session."""
        async with self._lifecycle_lock:
            await self._require_clean_module_runtime()
            try:
                await self._start_hardware("web")
            except Exception as exc:
                await self._publish_error(exc)
                try:
                    await self._stop_unlocked()
                except Exception as cleanup_error:
                    raise exc from cleanup_error
                raise

    async def _start_hardware(self, mode: str) -> None:
        if self._mode is not None or self.alsa is not None or self.pcm_bridge.running:
            raise ALSAUnavailable("QDC507 audio is already active")
        if self._module_runtime_started:
            raise ALSAUnavailable(
                "previous module voice cleanup was not confirmed; restart the gateway"
            )
        if self.module_runtime is not None:
            starter = getattr(self.module_runtime, "start_async", None)
            if callable(starter):
                await starter()
            else:
                await asyncio.to_thread(self.module_runtime.prepare_and_start)
            self._module_runtime_started = True
        endpoints = find_qdc507_pcm_devices(self.sysfs_root)
        captures = [item for item in endpoints if item.direction == "capture"]
        playbacks = [item for item in endpoints if item.direction == "playback"]
        if not captures or not playbacks:
            raise ALSAUnavailable("QDC507 full-duplex UAC endpoints were not found")
        self.alsa = AlsaPCMDevice(captures[0].name, playbacks[0].name)
        await asyncio.to_thread(self.alsa.open)
        self._stop.clear()
        await self.pcm_bridge.start()
        self._mode = mode
        self._session_started_at = time.monotonic()
        self._workers = [
            threading.Thread(target=self._capture_worker, name="audio-capture", daemon=True),
            threading.Thread(target=self._playback_worker, name="audio-playback", daemon=True),
        ]
        for worker in self._workers:
            worker.start()
        await self._publish("audio.state", {
            "state": "active",
            "mode": mode,
            "capture_device": captures[0].name,
            "playback_device": playbacks[0].name,
            "module_voice": self._module_voice_status(),
        })

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        was_active = (
            self.alsa is not None
            or self.pcm_bridge.running
            or self._module_runtime_started
        )
        self._stop.set()
        await self.pcm_bridge.stop()
        # Never close ALSA while a background read/write still owns its handle.
        for worker in self._workers:
            await asyncio.to_thread(worker.join, 2.0)
        if any(worker.is_alive() for worker in self._workers):
            raise ALSAUnavailable("audio workers did not stop; restart the gateway")
        self._workers.clear()
        errors: list[tuple[str, Exception]] = []
        session_summary = None
        if was_active:
            session_summary = {
                "mode": self._mode,
                "duration_ms": None if self._session_started_at is None else round(
                    max(0.0, time.monotonic() - self._session_started_at) * 1000,
                    3,
                ),
                "alsa": None if self.alsa is None else self.alsa.stats(),
                "bridge": self.pcm_bridge.stats(),
            }
        if self.alsa is not None:
            try:
                await asyncio.to_thread(self.alsa.close)
            except Exception as exc:
                errors.append(("alsa", exc))
        self.alsa = None
        if self._module_runtime_started and self.module_runtime is not None:
            stopper = getattr(self.module_runtime, "stop_async", None)
            try:
                if callable(stopper):
                    await stopper()
                else:
                    await asyncio.to_thread(self.module_runtime.stop_and_cleanup)
            except Exception as exc:
                errors.append(("module_voice", exc))
            else:
                self._module_runtime_started = False
        self._mode = None
        self._session_started_at = None
        if session_summary is not None:
            self._last_session = session_summary
        if was_active:
            await self._publish("audio.state", {
                "state": "stopped",
                "session": session_summary,
            })
        if errors:
            first_stage, first_error = errors[0]
            await self._publish("audio.cleanup_error", {
                "stage": first_stage,
                "error": type(first_error).__name__,
                "message": " ".join(str(first_error).split())[-500:],
                "count": len(errors),
            })
            raise first_error

    async def _require_clean_module_runtime(self) -> None:
        if not self._module_runtime_started:
            return
        error = ALSAUnavailable(
            "previous module voice cleanup was not confirmed; restart the gateway"
        )
        await self._publish_error(error)
        raise error

    async def _publish(self, event_type: str, payload: dict[str, object]) -> None:
        if self._event_publisher is None:
            return
        result = self._event_publisher(GatewayEvent(event_type, payload))
        if inspect.isawaitable(result):
            await result

    async def _publish_error(self, error: Exception) -> None:
        message = " ".join(str(error).split())[-500:]
        await self._publish("audio.error", {
            "error": type(error).__name__,
            "message": message or "audio startup failed",
            "module_voice": self._module_voice_status(),
        })

    def _module_voice_status(self) -> Optional[dict[str, object]]:
        if self.module_runtime is None:
            return None
        status = getattr(self.module_runtime, "status", None)
        if callable(status):
            return status()
        return {"configured": True, "active": self._module_runtime_started}

    def _capture_worker(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.alsa.read()
                if frame.data:
                    self.pcm_bridge.push_cellular(frame)
                else:
                    self._stop.wait(0.001)
            except Exception:
                self.pcm_bridge.record_xrun("cellular_to_client")
                self._stop.wait(0.005)

    def _playback_worker(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.pcm_bridge.pull_for_cellular()
                if frame is None:
                    self.alsa.write_silence()
                else:
                    self.alsa.write(frame)
            except Exception:
                self.pcm_bridge.record_xrun("client_to_cellular")
                self._stop.wait(0.005)

    def stats(self) -> dict[str, object]:
        return {
            "mode": self._mode,
            "alsa": None if self.alsa is None else self.alsa.stats(),
            "last_session": self._last_session,
            "bridge": self.pcm_bridge.stats(),
            "module_voice": self._module_voice_status(),
        }
