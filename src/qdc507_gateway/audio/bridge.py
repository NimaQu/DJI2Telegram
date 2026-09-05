from __future__ import annotations

import asyncio
import inspect
import math
import struct
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .alsa import AlsaPCMDevice, ALSAUnavailable, find_qdc507_pcm_devices
from .ring import PCMFrame, RingBuffer
from qdc507_gateway.models import GatewayEvent


class PCMBridge:
    """Bounded PCM boundary between the ALSA and Telegram media loops.

    The media engines own their I/O callbacks; this class owns only bounded
    buffering and accounting. It never blocks indefinitely and never grows
    memory when one leg stops consuming frames.
    """

    def __init__(self, capacity: int = 50):
        self.cellular_to_telegram = RingBuffer(capacity)
        self.telegram_to_cellular = RingBuffer(capacity)
        self.running = False
        self._telegram_cue_lock = threading.Lock()
        self._telegram_cue: Optional[dict[str, float | int]] = None

    async def start(self, _telegram_handle=None) -> None:
        self.cellular_to_telegram.reset()
        self.telegram_to_cellular.reset()
        self._clear_telegram_cue()
        self.running = True

    async def stop(self) -> None:
        self.running = False
        self._clear_telegram_cue()
        self.cellular_to_telegram.clear()
        self.telegram_to_cellular.clear()

    def push_cellular(self, frame: PCMFrame) -> bool:
        if not self.running:
            return False
        self.cellular_to_telegram.put(frame)
        return True

    def push_telegram(self, frame: PCMFrame) -> bool:
        if not self.running:
            return False
        self.telegram_to_cellular.put(frame)
        return True

    def pull_for_telegram(self) -> Optional[PCMFrame]:
        return self.cellular_to_telegram.get()

    def pull_for_cellular(self) -> Optional[PCMFrame]:
        return self.telegram_to_cellular.get()

    def queue_telegram_cue(
        self,
        *,
        frequency_hz: float = 880.0,
        duration_ms: int = 120,
        amplitude: float = 0.18,
    ) -> bool:
        """Arm a short tone mixed only into audio sent toward Telegram."""
        if not self.running:
            return False
        if frequency_hz <= 0 or duration_ms <= 0 or not 0 < amplitude <= 1:
            raise ValueError("invalid Telegram cue parameters")
        with self._telegram_cue_lock:
            self._telegram_cue = {
                "frequency_hz": frequency_hz,
                "duration_ms": duration_ms,
                "amplitude": amplitude,
                "position": 0,
            }
        return True

    def mix_telegram_cue(self, data: bytes, sample_rate: int) -> bytes:
        """Mix the pending cue into one PCM16 mono frame without touching cellular audio."""
        if sample_rate <= 0 or len(data) % 2:
            raise ValueError("invalid PCM16 Telegram cue frame")
        with self._telegram_cue_lock:
            cue = self._telegram_cue
            if cue is None:
                return data
            total = max(1, round(sample_rate * int(cue["duration_ms"]) / 1000))
            position = int(cue["position"])
            fade_samples = max(1, min(round(sample_rate * 0.005), total // 4))
            output = bytearray(data)
            for offset in range(0, len(output), 2):
                if position >= total:
                    break
                attack = min(1.0, (position + 1) / fade_samples)
                release = min(1.0, (total - position) / fade_samples)
                envelope = min(attack, release)
                phase = 2 * math.pi * float(cue["frequency_hz"]) * position / sample_rate
                tone = round(float(cue["amplitude"]) * 32767 * envelope * math.sin(phase))
                original = struct.unpack_from("<h", output, offset)[0]
                mixed = max(-32768, min(32767, original + tone))
                struct.pack_into("<h", output, offset, mixed)
                position += 1
            cue["position"] = position
            if position >= total:
                self._telegram_cue = None
            return bytes(output)

    def _clear_telegram_cue(self) -> None:
        with self._telegram_cue_lock:
            self._telegram_cue = None

    def stats(self) -> dict[str, object]:
        return {
            "running": self.running,
            "cellular_to_telegram": self.cellular_to_telegram.stats(),
            "telegram_to_cellular": self.telegram_to_cellular.stats(),
        }

    def record_xrun(self, direction: str = "cellular_to_telegram") -> None:
        """Record an overrun/underrun on the named bounded audio leg."""
        if direction == "cellular_to_telegram":
            self.cellular_to_telegram.record_xrun()
        elif direction == "telegram_to_cellular":
            self.telegram_to_cellular.record_xrun()
        else:
            raise ValueError("unknown PCM direction")


class AlsaNTgCallsAudioAdapter:
    """Connect QDC507 UAC/ALSA frames to a Kurigram PyTgCalls bridge."""

    def __init__(
        self,
        telegram_bridge_getter: Callable[[], Any],
        sysfs_root: str | Path = "/sys",
        event_publisher: Optional[Callable[[GatewayEvent], Awaitable[Any]]] = None,
        module_runtime: Any = None,
        vendor_id: int = 0x2C7C,
        product_id: int = 0x0125,
    ):
        self.telegram_bridge_getter = telegram_bridge_getter
        self.sysfs_root = sysfs_root
        self.vendor_id = vendor_id
        self.product_id = product_id
        # Ten 20 ms frames cap one-way queueing at about 200 ms. A one-second
        # buffer made browser-to-cellular latency approach 1,000 ms under
        # normal WebAudio jitter and is not useful for an interactive call.
        self.pcm_bridge = PCMBridge(capacity=10)
        self.alsa: Optional[AlsaPCMDevice] = None
        self.binding = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._event_publisher = event_publisher
        self.module_runtime = module_runtime
        self._module_runtime_started = False
        self._mode: Optional[str] = None
        self._lifecycle_lock = asyncio.Lock()
        self._session_started_at: Optional[float] = None
        self._last_session: Optional[dict[str, object]] = None

    def stream(self) -> Any:
        bridge = self.telegram_bridge_getter()
        if bridge is None:
            raise ALSAUnavailable("Kurigram call bridge is unavailable")
        return bridge.external_audio_stream()

    async def start(self, handle: Any) -> None:
        async with self._lifecycle_lock:
            await self._require_clean_module_runtime()
            try:
                bridge = self.telegram_bridge_getter()
                if bridge is None:
                    raise ALSAUnavailable("Kurigram call bridge is unavailable")
                await self._start_hardware("telegram")
                chat_id = getattr(handle, "user_id", handle)
                self.binding = await bridge.attach_pcm(chat_id, self.pcm_bridge)
            except Exception as exc:
                await self._publish_error(exc)
                try:
                    await self._stop_unlocked()
                except Exception as cleanup_error:
                    raise exc from cleanup_error
                raise

    async def start_web(self, _call_id: str) -> None:
        """Open QDC507 UAC without attaching a Telegram media engine."""
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

    async def play_telegram_dial_cue(self) -> bool:
        """Play a local confirmation only after an outbound cellular dial succeeds."""
        if self._mode != "telegram":
            return False
        queued = self.pcm_bridge.queue_telegram_cue()
        if queued:
            await self._publish("audio.cue", {
                "target": "telegram",
                "kind": "cellular_dial_started",
            })
        return queued

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
        endpoints = find_qdc507_pcm_devices(self.sysfs_root, self.vendor_id, self.product_id)
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
        self._tasks = [
            asyncio.create_task(self._capture_loop()),
            asyncio.create_task(self._playback_loop()),
        ]
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
            or self.binding is not None
            or self.pcm_bridge.running
            or self._module_runtime_started
        )
        self._stop.set()
        await self.pcm_bridge.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
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
        try:
            bridge = self.telegram_bridge_getter()
        except Exception:
            bridge = None
        if bridge is not None and self.binding is not None:
            try:
                await bridge.detach_pcm(self.binding)
            except Exception as exc:
                errors.append(("telegram_pcm", exc))
        self.binding = None
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

    async def _capture_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = await asyncio.to_thread(self.alsa.read)
                self.pcm_bridge.push_cellular(frame)
            except asyncio.CancelledError:
                return
            except Exception:
                self.pcm_bridge.record_xrun("cellular_to_telegram")
                await asyncio.sleep(0.005)

    async def _playback_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.pcm_bridge.pull_for_cellular()
                if frame is None:
                    # A UAC playback PCM underruns if browser/WebRTC jitter
                    # leaves even a short gap. The blocking ALSA write is also
                    # our 20 ms clock, so fill a missing period with silence
                    # instead of letting the device repeatedly stop/recover.
                    await asyncio.to_thread(self.alsa.write_silence)
                    continue
                await asyncio.to_thread(self.alsa.write, frame)
            except asyncio.CancelledError:
                return
            except Exception:
                self.pcm_bridge.record_xrun("telegram_to_cellular")
                await asyncio.sleep(0.005)

    def stats(self) -> dict[str, object]:
        return {
            "mode": self._mode,
            "alsa": None if self.alsa is None else self.alsa.stats(),
            "last_session": self._last_session,
            "bridge": self.pcm_bridge.stats(),
            "module_voice": self._module_voice_status(),
        }
