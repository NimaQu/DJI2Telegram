from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from starlette.websockets import WebSocketDisconnect

from qdc507_gateway.audio.alsa import resample_pcm16_mono
from qdc507_gateway.audio.ring import PCMFrame
from qdc507_gateway.models import CallDirection, CallRecord, CallState
from qdc507_gateway.telegram.calls import CallBridgeError, CallCoordinator


AUDIO_SUBPROTOCOL = "qdc507.audio.v1"
AUDIO_TICKET_PREFIX = "ticket."
AUDIO_SAMPLE_RATE = 8000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH = 2
AUDIO_FRAME_SAMPLES = 160
AUDIO_FRAME_BYTES = AUDIO_FRAME_SAMPLES * AUDIO_SAMPLE_WIDTH
MAX_AUDIO_MESSAGE_BYTES = AUDIO_FRAME_BYTES * 10
PUBLIC_CALL_ERROR_TYPES = {"CallBridgeError", "ModuleServiceError"}


def public_call_error(error: Exception) -> str:
    """Return a bounded diagnostic without exposing arbitrary exception text."""
    name = type(error).__name__
    if name not in PUBLIC_CALL_ERROR_TYPES:
        return name
    message = " ".join(str(error).split())
    if not message:
        return name
    return f"{name}: {message[:240]}"


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
        self._tickets: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("ascii")).hexdigest()

    def issue(self, call_id: str) -> dict[str, object]:
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call id is required")
        now = time.monotonic()
        with self._lock:
            self._remove_expired(now)
            if len(self._tickets) >= self.maximum:
                raise RuntimeError("too many pending audio tickets")
            ticket = secrets.token_urlsafe(32)
            self._tickets[self._key(ticket)] = (call_id, now + self.ttl_seconds)
        return {
            "ticket": ticket,
            "expires_in": self.ttl_seconds,
            "subprotocol": AUDIO_SUBPROTOCOL,
        }

    def consume(self, call_id: str, ticket: str) -> bool:
        if not isinstance(call_id, str) or not isinstance(ticket, str):
            return False
        now = time.monotonic()
        try:
            key = self._key(ticket)
        except (UnicodeEncodeError, AttributeError):
            return False
        with self._lock:
            self._remove_expired(now)
            value = self._tickets.pop(key, None)
        return value is not None and value[0] == call_id and value[1] >= now

    def _remove_expired(self, now: float) -> None:
        expired = [key for key, (_, deadline) in self._tickets.items() if deadline < now]
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


class WebCallController:
    """Cellular call state machine controlled by the authenticated web API."""

    def __init__(
        self,
        coordinator: CallCoordinator,
        cellular_dial: Callable[[str], Awaitable[Any]],
        cellular_answer: Callable[[], Awaitable[Any]],
        cellular_hangup: Callable[[], Awaitable[Any]],
        audio_start: Callable[[str], Awaitable[Any]],
        audio_stop: Callable[[], Awaitable[Any]],
        record_sink: Optional[Callable[[CallRecord], Awaitable[Any]]] = None,
        timeout_seconds: float = 60.0,
    ):
        self.coordinator = coordinator
        self.cellular_dial = cellular_dial
        self.cellular_answer = cellular_answer
        self.cellular_hangup = cellular_hangup
        self.audio_start = audio_start
        self.audio_stop = audio_stop
        self.record_sink = record_sink
        self.timeout_seconds = timeout_seconds
        self._audio_attached = False
        self._cellular_started = False
        self._cellular_connected = False
        self._timeout_task: Optional[asyncio.Task] = None
        self._operation_lock = asyncio.Lock()
        self._hangup_lock = asyncio.Lock()
        self._call_end_id: Optional[str] = None
        self._call_end_event: Optional[asyncio.Event] = None

    async def start_outbound(self, number: str) -> CallRecord:
        async with self._operation_lock:
            record = await self.coordinator.start_outbound(
                number,
                None,
                frontend="web",
                initial_state=CallState.waiting_client,
            )
            self._begin_call(record.id)
            self._reset_flags()
            await self._record(record)
            self._arm_timeout(record.id)
            return record

    async def start_inbound(self, number: Optional[str]) -> CallRecord:
        async with self._operation_lock:
            current = await self.coordinator.current()
            if (
                current is not None
                and current.frontend == "web"
                and current.direction == CallDirection.inbound_cellular
            ):
                if current.cellular_number is None and number is not None:
                    current.cellular_number = number
                    await self._record(current)
                return current
            record = await self.coordinator.start_inbound(
                number,
                None,
                frontend="web",
                initial_state=CallState.ringing_cellular,
            )
            self._begin_call(record.id)
            self._reset_flags()
            self._cellular_started = True
            await self._record(record)
            self._arm_timeout(record.id)
            return record

    async def require_call(self, call_id: str) -> CallRecord:
        record = await self.coordinator.current()
        if record is None or record.id != call_id or record.frontend != "web":
            raise CallBridgeError("web call id does not match the active call")
        return record

    async def wait_ended(self, call_id: str) -> None:
        """Wait until the named web call no longer owns the shared audio bridge."""
        if self._call_end_id != call_id or self._call_end_event is None:
            return
        event = self._call_end_event
        await event.wait()

    async def attach_audio(self, call_id: str) -> CallRecord:
        async with self._operation_lock:
            record = await self.require_call(call_id)
            if self._audio_attached:
                raise CallBridgeError("web audio is already attached")
            try:
                await self.audio_start(call_id)
                self._audio_attached = True
                if record.direction == CallDirection.outbound_cellular:
                    # Mark this before ATD: a transport detach after a possibly
                    # accepted command must still clean up the cellular leg.
                    self._cellular_started = True
                    await self.cellular_dial(record.cellular_number or "")
                    record = await self.coordinator.transition(CallState.waiting_cellular)
                    await self._record(record)
                return record
            except Exception as exc:
                await self._fail_unlocked(exc)
                raise

    async def answer(self, call_id: str) -> CallRecord:
        async with self._operation_lock:
            record = await self.require_call(call_id)
            if record.direction != CallDirection.inbound_cellular:
                raise CallBridgeError("only an incoming cellular call can be answered")
            if not self._audio_attached:
                raise CallBridgeError("web audio must be connected before answering")
            try:
                await self.cellular_answer()
                self._cellular_connected = True
                self._cancel_timeout()
                record = await self.coordinator.transition(CallState.active)
                await self._record(record)
                return record
            except Exception as exc:
                await self._fail_unlocked(exc)
                raise

    async def cellular_connected(self) -> Optional[CallRecord]:
        async with self._operation_lock:
            record = await self.coordinator.current()
            if record is None or record.frontend != "web":
                return record
            self._cellular_connected = True
            if not self._audio_attached:
                return record
            self._cancel_timeout()
            record = await self.coordinator.transition(CallState.active)
            await self._record(record)
            return record

    async def cellular_disconnected(self) -> Optional[CallRecord]:
        return await self.hangup(reason="cellular call disconnected")

    async def websocket_disconnected(self, call_id: str) -> Optional[CallRecord]:
        record = await self.coordinator.current()
        if record is None or record.id != call_id or record.frontend != "web":
            return record
        return await self.hangup(call_id, reason="web audio disconnected")

    async def hangup(
        self,
        call_id: Optional[str] = None,
        reason: str = "hangup",
    ) -> Optional[CallRecord]:
        async with self._hangup_lock:
            async with self._operation_lock:
                record = await self.coordinator.current()
                if record is None:
                    return None
                if record.frontend != "web":
                    raise CallBridgeError("active call is not controlled by the web frontend")
                if call_id is not None and record.id != call_id:
                    raise CallBridgeError("call id does not match the active call")
                errors = await self._cleanup_unlocked()
                error_text = reason if reason != "hangup" else None
                if errors:
                    error_text = (error_text + "; " if error_text else "") + "call cleanup error"
                record = await self.coordinator.transition(CallState.ended, error_text)
                await self._record(record)
                return record

    async def _fail_unlocked(self, error: Exception) -> None:
        await self._cleanup_unlocked()
        record = await self.coordinator.current()
        if record is not None and record.frontend == "web":
            record = await self.coordinator.transition(
                CallState.failed,
                public_call_error(error),
            )
            await self._record(record)

    async def _cleanup_unlocked(self) -> list[Exception]:
        self._cancel_timeout()
        self._mark_call_ended()
        errors: list[Exception] = []
        if self._audio_attached:
            try:
                await self.audio_stop()
            except Exception as exc:
                errors.append(exc)
        if self._cellular_started:
            try:
                await self.cellular_hangup()
            except Exception as exc:
                errors.append(exc)
        self._reset_flags()
        return errors

    def _reset_flags(self) -> None:
        self._audio_attached = False
        self._cellular_started = False
        self._cellular_connected = False

    def _begin_call(self, call_id: str) -> None:
        self._call_end_id = call_id
        self._call_end_event = asyncio.Event()

    def _mark_call_ended(self) -> None:
        if self._call_end_event is not None:
            self._call_end_event.set()

    async def _record(self, record: CallRecord) -> None:
        if self.record_sink is not None:
            result = self.record_sink(record)
            if inspect.isawaitable(result):
                await result

    def _arm_timeout(self, call_id: str) -> None:
        self._cancel_timeout()
        self._timeout_task = asyncio.create_task(self._timeout(call_id))

    def _cancel_timeout(self) -> None:
        task = self._timeout_task
        self._timeout_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _timeout(self, call_id: str) -> None:
        try:
            await asyncio.sleep(self.timeout_seconds)
            await self.hangup(call_id, reason="call timeout")
        except asyncio.CancelledError:
            return
        except CallBridgeError:
            return


class WebAudioSession:
    """Move fixed 20 ms PCM16/8 kHz mono frames across one WebSocket."""

    def __init__(
        self,
        controller: WebCallController,
        audio_adapter: Any,
        startup_timeout_seconds: float = 3.0,
    ):
        self.controller = controller
        self.audio_adapter = audio_adapter
        self.startup_timeout_seconds = startup_timeout_seconds
        self.frames_to_browser = 0
        self.frames_from_browser = 0
        self.invalid_messages = 0

    async def run(self, websocket: Any, call_id: str) -> None:
        try:
            initial_frames = await self._receive_initial_audio(websocket)
            await self.controller.attach_audio(call_id)
            for frame in initial_frames:
                self.audio_adapter.pcm_bridge.push_telegram(frame)
            await self.stream(websocket, call_id, session_type="call")
        finally:
            await self.controller.websocket_disconnected(call_id)

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
        sender = asyncio.create_task(self._send_audio(websocket))
        receiver = asyncio.create_task(self._receive_audio(websocket))
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

    async def _send_audio(self, websocket: Any) -> None:
        pending = bytearray()
        while True:
            frame = self.audio_adapter.pcm_bridge.pull_for_telegram()
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
                self.frames_to_browser += 1

    async def _receive_audio(self, websocket: Any) -> None:
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
                for offset in range(0, len(data), AUDIO_FRAME_BYTES):
                    accepted = self.audio_adapter.pcm_bridge.push_telegram(PCMFrame(
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
