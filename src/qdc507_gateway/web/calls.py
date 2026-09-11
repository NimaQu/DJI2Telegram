from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any, Optional

from starlette.websockets import WebSocketDisconnect

from qdc507_gateway.audio.recording import DebugRecordings
from qdc507_gateway.audio.alsa import resample_pcm16_mono
from qdc507_gateway.audio.ring import PCMFrame
from qdc507_gateway.calls.core import CallBridgeError, CallCoordinator
from qdc507_gateway.calls.controller import ClientCallController as WebCallController


AUDIO_SUBPROTOCOL = "qdc507.audio.v1"
AUDIO_TICKET_PREFIX = "ticket."
AUDIO_SAMPLE_RATE = 8000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH = 2
AUDIO_FRAME_SAMPLES = 160
AUDIO_FRAME_BYTES = AUDIO_FRAME_SAMPLES * AUDIO_SAMPLE_WIDTH
MAX_AUDIO_MESSAGE_BYTES = AUDIO_FRAME_BYTES * 10


class AudioTicketStore:
    """Short-lived, single-use WebSocket credentials bound to one call.

    Native browser WebSockets cannot add an Authorization header.  A normal
    Bearer-authenticated REST request therefore issues a one-time credential,
    which is sent as a WebSocket subprotocol token and never persisted.
    """

    def __init__(self, ttl_seconds: float = 30.0, maximum: int = 256):
        if ttl_seconds <= 0 or maximum <= 0:
            raise ValueError("ticket lifetime and capacity must be positive")
        self.ttl_seconds = ttl_seconds
        self.maximum = maximum
        self._tickets: dict[str, tuple[str, float, Optional[str]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("ascii")).hexdigest()

    def issue(self, call_id: str, installation_id: Optional[str] = None) -> dict[str, object]:
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call id is required")
        now = time.monotonic()
        with self._lock:
            self._remove_expired(now)
            if len(self._tickets) >= self.maximum:
                raise RuntimeError("too many pending audio tickets")
            ticket = secrets.token_urlsafe(32)
            self._tickets[self._key(ticket)] = (call_id, now + self.ttl_seconds, installation_id)
        return {
            "ticket": ticket,
            "expires_in": self.ttl_seconds,
            "subprotocol": AUDIO_SUBPROTOCOL,
        }

    def consume(self, call_id: str, ticket: str) -> bool:
        return self.take(call_id, ticket) is not None

    def take(self, call_id: str, ticket: str):
        if not isinstance(call_id, str) or not isinstance(ticket, str):
            return None
        now = time.monotonic()
        try:
            key = self._key(ticket)
        except (UnicodeEncodeError, AttributeError):
            return None
        with self._lock:
            self._remove_expired(now)
            value = self._tickets.pop(key, None)
        if value is None or value[0] != call_id or value[1] < now:
            return None
        return {"installation_id": value[2]}

    def _remove_expired(self, now: float) -> None:
        expired = [key for key, (_, deadline, _) in self._tickets.items() if deadline < now]
        for key in expired:
            self._tickets.pop(key, None)


def extract_audio_ticket(protocol_header: Optional[str]) -> Optional[str]:
    if not protocol_header:
        return None
    protocols = [item.strip() for item in protocol_header.split(",")]
    if AUDIO_SUBPROTOCOL not in protocols:
        return None
    tickets = [
        item[len(AUDIO_TICKET_PREFIX):]
        for item in protocols
        if item.startswith(AUDIO_TICKET_PREFIX)
    ]
    if len(tickets) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", tickets[0]):
        return None
    return tickets[0]




class WebAudioSession:
    """Move fixed 20 ms PCM16/8 kHz mono frames across one WebSocket."""

    def __init__(
        self,
        controller: WebCallController,
        audio_adapter: Any,
        startup_timeout_seconds: float = 3.0,
        debug_recording_enabled: bool = False,
        recording_directory=None,
    ):
        self.controller = controller
        self.audio_adapter = audio_adapter
        self.startup_timeout_seconds = startup_timeout_seconds
        self.frames_to_browser = 0
        self.frames_from_browser = 0
        self.invalid_messages = 0
        self.recordings = DebugRecordings(debug_recording_enabled, recording_directory)

    async def run(self, websocket: Any, call_id: str, installation_id: Optional[str] = None) -> None:
        reserve = getattr(self.controller, "reserve_audio", None)
        if callable(reserve):
            # Reservation fails before the cleanup block: a second socket must
            # never hang up the first socket's call.
            await reserve(call_id, installation_id)
        self.recordings.start(call_id)
        try:
            if installation_id is not None:
                # CallKit has activated AVAudioSession; do not wait for a PCM
                # frame to report readiness or answer the cellular leg.
                await self.controller.attach_audio(call_id, installation_id)
            else:
                initial_frames = await self._receive_initial_audio(websocket)
                await self.controller.attach_audio(call_id)
                for frame in initial_frames:
                    self.recordings.append(call_id, "client_to_bridge", frame.data)
                    self.audio_adapter.pcm_bridge.push_client(frame)
            await self.stream(websocket, call_id, session_type="call")
        finally:
            try:
                await self.controller.websocket_disconnected(call_id)
            finally:
                await self.recordings.finish(call_id)

    async def _receive_initial_audio(self, websocket: Any) -> list[PCMFrame]:
        """Require browser microphone PCM before ALSA startup and cellular dialing."""
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CallBridgeError("browser microphone produced no PCM frames")
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise CallBridgeError("browser microphone produced no PCM frames") from exc
            if message.get("type") == "websocket.disconnect":
                raise CallBridgeError("web audio disconnected before microphone startup")
            data = message.get("bytes")
            if data is not None:
                if (
                    not data
                    or len(data) > MAX_AUDIO_MESSAGE_BYTES
                    or len(data) % AUDIO_FRAME_BYTES
                ):
                    self.invalid_messages += 1
                    await websocket.close(code=1003, reason="invalid PCM frame size")
                    raise CallBridgeError("browser sent an invalid PCM frame")
                frames = [
                    PCMFrame(
                        data[offset:offset + AUDIO_FRAME_BYTES],
                        AUDIO_SAMPLE_RATE,
                        AUDIO_CHANNELS,
                        AUDIO_SAMPLE_WIDTH,
                    )
                    for offset in range(0, len(data), AUDIO_FRAME_BYTES)
                ]
                self.frames_from_browser += len(frames)
                return frames
            text = message.get("text")
            if text is None:
                continue
            try:
                command = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                self.invalid_messages += 1
                continue
            if isinstance(command, dict) and command.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    async def stream(
        self,
        websocket: Any,
        session_id: str,
        *,
        session_type: str,
    ) -> None:
        await websocket.send_json({
            "type": "ready",
            "session_id": session_id,
            "session_type": session_type,
            "format": {
                "encoding": "pcm_s16le",
                "sample_rate": AUDIO_SAMPLE_RATE,
                "channels": AUDIO_CHANNELS,
                "frame_ms": 20,
                "frame_bytes": AUDIO_FRAME_BYTES,
            },
        })
        sender = asyncio.create_task(self._send_audio(websocket, session_id))
        receiver = asyncio.create_task(self._receive_audio(websocket, session_id))
        call_ended = None
        wait_ended = getattr(self.controller, "wait_ended", None)
        if session_type == "call" and callable(wait_ended):
            call_ended = asyncio.create_task(wait_ended(session_id))
        tasks = [sender, receiver]
        if call_ended is not None:
            tasks.append(call_ended)
        try:
            done, _pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            ended_by_call = call_ended is not None and call_ended in done
            if ended_by_call:
                try:
                    await websocket.close(code=1000, reason="call ended")
                except (RuntimeError, WebSocketDisconnect):
                    pass
            if not ended_by_call:
                for task in done:
                    if task is call_ended or task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None:
                        raise error
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_audio(self, websocket: Any, session_id=None) -> None:
        pending = bytearray()
        while True:
            frame = self.audio_adapter.pcm_bridge.pull_for_client()
            if frame is None:
                await asyncio.sleep(0.005)
                continue
            if frame.channels != AUDIO_CHANNELS or frame.sample_width != AUDIO_SAMPLE_WIDTH:
                self.invalid_messages += 1
                continue
            pending.extend(resample_pcm16_mono(
                frame.data,
                frame.sample_rate,
                AUDIO_SAMPLE_RATE,
            ))
            while len(pending) >= AUDIO_FRAME_BYTES:
                chunk = bytes(pending[:AUDIO_FRAME_BYTES])
                del pending[:AUDIO_FRAME_BYTES]
                await websocket.send_bytes(chunk)
                self.recordings.append(session_id, "bridge_to_client", chunk)
                self.frames_to_browser += 1

    async def _receive_audio(self, websocket: Any, session_id=None) -> None:
        while True:
            message = await websocket.receive()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                if (
                    not data
                    or len(data) > MAX_AUDIO_MESSAGE_BYTES
                    or len(data) % AUDIO_FRAME_BYTES
                ):
                    self.invalid_messages += 1
                    await websocket.close(code=1003, reason="invalid PCM frame size")
                    return
                self.recordings.append(session_id, "client_to_bridge", data)
                for offset in range(0, len(data), AUDIO_FRAME_BYTES):
                    accepted = self.audio_adapter.pcm_bridge.push_client(PCMFrame(
                        data[offset:offset + AUDIO_FRAME_BYTES],
                        AUDIO_SAMPLE_RATE,
                        AUDIO_CHANNELS,
                        AUDIO_SAMPLE_WIDTH,
                    ))
                    if accepted:
                        self.frames_from_browser += 1
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                command = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                self.invalid_messages += 1
                continue
            if isinstance(command, dict) and command.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    def stats(self) -> dict[str, int]:
        return {
            "frames_to_browser": self.frames_to_browser,
            "frames_from_browser": self.frames_from_browser,
            "invalid_messages": self.invalid_messages,
        }


class WebAudioDiagnosticService:
    """Run the browser/ALSA PCM path without answering or dialing a call."""

    def __init__(
        self,
        coordinator: CallCoordinator,
        audio_adapter: Any,
        audio_session: WebAudioSession,
        tickets: AudioTicketStore,
        reservation_seconds: float = 45.0,
    ):
        self.coordinator = coordinator
        self.audio_adapter = audio_adapter
        self.audio_session = audio_session
        self.tickets = tickets
        self.reservation_seconds = reservation_seconds
        self._session_id: Optional[str] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._expiry_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _ticket_identity(session_id: str) -> str:
        return "diagnostic:" + session_id

    @property
    def active(self) -> bool:
        return self._session_id is not None

    async def create(self) -> dict[str, object]:
        async with self._lock:
            if await self.coordinator.current() is not None:
                raise CallBridgeError("audio diagnostic is unavailable during a call")
            if self._session_id is not None:
                raise CallBridgeError("another audio diagnostic is already active")
            self._session_id = str(uuid.uuid4())
            self._arm_expiry(self._session_id)
            return {
                "id": self._session_id,
                "state": "waiting_websocket",
                "expires_in": self.reservation_seconds,
            }

    async def require(self, session_id: str) -> None:
        async with self._lock:
            if self._session_id != session_id:
                raise CallBridgeError("audio diagnostic session is not active")

    async def issue_ticket(self, session_id: str) -> dict[str, object]:
        await self.require(session_id)
        return self.tickets.issue(self._ticket_identity(session_id))

    async def consume_ticket(self, session_id: str, ticket: str) -> bool:
        try:
            await self.require(session_id)
        except CallBridgeError:
            return False
        return self.tickets.consume(self._ticket_identity(session_id), ticket)

    async def run(self, websocket: Any, session_id: str) -> None:
        current_task = asyncio.current_task()
        async with self._lock:
            if self._session_id != session_id or self._stream_task is not None:
                raise CallBridgeError("audio diagnostic session is unavailable")
            self._stream_task = current_task
            self._cancel_expiry()
        started = False
        try:
            await self.audio_adapter.start_web(self._ticket_identity(session_id))
            started = True
            await self.audio_session.stream(
                websocket,
                session_id,
                session_type="diagnostic",
            )
        finally:
            try:
                if started:
                    await self.audio_adapter.stop()
            finally:
                async with self._lock:
                    if self._session_id == session_id:
                        self._session_id = None
                    if self._stream_task is current_task:
                        self._stream_task = None
                    self._cancel_expiry()

    async def stop(self) -> None:
        async with self._lock:
            task = self._stream_task
            self._session_id = None
            self._stream_task = None
            self._cancel_expiry()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _arm_expiry(self, session_id: str) -> None:
        self._cancel_expiry()
        self._expiry_task = asyncio.create_task(self._expire(session_id))

    def _cancel_expiry(self) -> None:
        task = self._expiry_task
        self._expiry_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _expire(self, session_id: str) -> None:
        try:
            await asyncio.sleep(self.reservation_seconds)
            async with self._lock:
                if self._session_id == session_id and self._stream_task is None:
                    self._session_id = None
                self._expiry_task = None
        except asyncio.CancelledError:
            return
