from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from qdc507_gateway.models import CallDirection, CallRecord, CallState


class CallBusyError(RuntimeError):
    pass


@dataclass
class CallCoordinator:
    """Transport-independent call state machine."""

    active: Optional[CallRecord] = None
    lock: asyncio.Lock = None  # type: ignore

    def __post_init__(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()

    async def start_outbound(
        self,
        number: str,
        telegram_user_id: Optional[int],
        *,
        frontend: str = "telegram",
        initial_state: CallState = CallState.waiting_telegram,
    ) -> CallRecord:
        async with self.lock:
            if self.active and self.active.state not in (CallState.ended, CallState.failed):
                raise CallBusyError("another call is already active")
            self.active = CallRecord(
                id=str(uuid.uuid4()), direction=CallDirection.outbound_cellular,
                state=initial_state, cellular_number=number,
                telegram_user_id=telegram_user_id, frontend=frontend,
            )
            return self.active

    async def start_inbound(
        self,
        number: Optional[str],
        telegram_user_id: Optional[int],
        *,
        frontend: str = "telegram",
        initial_state: CallState = CallState.waiting_telegram,
    ) -> CallRecord:
        async with self.lock:
            if self.active and self.active.state not in (CallState.ended, CallState.failed):
                raise CallBusyError("another call is already active")
            self.active = CallRecord(
                id=str(uuid.uuid4()), direction=CallDirection.inbound_cellular,
                state=initial_state, cellular_number=number,
                telegram_user_id=telegram_user_id, frontend=frontend,
            )
            return self.active

    async def transition(self, state: CallState, error: Optional[str] = None) -> CallRecord:
        async with self.lock:
            if self.active is None:
                raise RuntimeError("no active call")
            self.active.state = state
            self.active.last_error = error
            if state == CallState.active and self.active.connected_at is None:
                from qdc507_gateway.models import utc_now
                self.active.connected_at = utc_now()
            if state in (CallState.ended, CallState.failed):
                from qdc507_gateway.models import utc_now
                self.active.ended_at = utc_now()
            return self.active

    async def current(self) -> Optional[CallRecord]:
        async with self.lock:
            if self.active is None or self.active.state in (CallState.ended, CallState.failed):
                return None
            return self.active


class CallBridgeError(RuntimeError):
    pass


@dataclass
class CallBridgeOrchestrator:
    """Coordinate the two call legs and guarantee symmetric cleanup.

    The callbacks deliberately represent transport boundaries. The cellular
    side can be backed by AT/URCs, the Telegram side by Kurigram/PyTgCalls,
    and the audio side by ALSA/NTgCalls. No transport is claimed or started by
    this state machine itself.
    """

    coordinator: CallCoordinator
    user_id: int
    request_telegram: Callable[[int], Awaitable[Any]]
    telegram_hangup: Callable[[Any], Awaitable[Any]]
    cellular_dial: Callable[[str], Awaitable[Any]]
    cellular_answer: Callable[[], Awaitable[Any]]
    cellular_hangup: Callable[[], Awaitable[Any]]
    audio_start: Callable[[Any], Awaitable[Any]]
    audio_stop: Callable[[], Awaitable[Any]]
    audio_dial_cue: Optional[Callable[[], Awaitable[Any]]] = None
    record_sink: Optional[Callable[[CallRecord], Awaitable[Any]]] = None
    timeout_seconds: float = 45.0
    telegram_handle: Any = None
    _telegram_connected: bool = False
    _cellular_connected: bool = False
    _audio_started: bool = False
    _cellular_started: bool = False
    _timeout_task: Optional[asyncio.Task] = None
    _hangup_lock: asyncio.Lock = None  # type: ignore

    def __post_init__(self) -> None:
        if self._hangup_lock is None:
            self._hangup_lock = asyncio.Lock()

    async def start_outbound(self, number: str, telegram_user_id: Optional[int] = None) -> CallRecord:
        record = await self.coordinator.start_outbound(number, telegram_user_id or self.user_id)
        await self._record(record)
        try:
            self.telegram_handle = await self.request_telegram(record.telegram_user_id or self.user_id)
            self._arm_timeout(record.id)
            return record
        except Exception as exc:
            await self._fail_start(record, exc)
            raise CallBridgeError("could not start Telegram call") from exc

    async def start_inbound(self, number: Optional[str]) -> CallRecord:
        record = await self.coordinator.start_inbound(number, self.user_id)
        await self._record(record)
        self._cellular_started = True
        try:
            self.telegram_handle = await self.request_telegram(self.user_id)
            self._arm_timeout(record.id)
            return record
        except Exception as exc:
            await self._fail_start(record, exc)
            raise CallBridgeError("could not notify Telegram of cellular call") from exc

    async def telegram_connected(self) -> CallRecord:
        record = await self._require_active()
        if self._telegram_connected:
            return record
        self._telegram_connected = True
        # Bring up the module voice route, ALSA and the NTgCalls PCM binding
        # before touching the cellular leg.  Starting these only after CLCC
        # reports ACTIVE clips the beginning of IVR/voicemail audio while the
        # module route is still being prepared.
        await self._start_audio_if_needed()
        if record.direction == CallDirection.outbound_cellular:
            self._cellular_started = True
            await self.cellular_dial(record.cellular_number or "")
            if self.audio_dial_cue is not None:
                try:
                    await self.audio_dial_cue()
                except Exception:
                    # A local confirmation cue must never tear down a call
                    # after the modem has already accepted ATD.
                    pass
        else:
            await self.cellular_answer()
            self._cellular_connected = True
        return await self._activate_if_ready()

    async def cellular_connected(self) -> CallRecord:
        record = await self._require_active()
        if self._cellular_connected:
            return record
        self._cellular_connected = True
        return await self._activate_if_ready()

    async def hangup(self, call_id: Optional[str] = None, reason: str = "hangup") -> Optional[CallRecord]:
        async with self._hangup_lock:
            record = await self._require_active(optional=True)
            if record is None:
                return None
            if call_id is not None and call_id != record.id:
                raise CallBridgeError("call id does not match the active call")
            errors = await self._cleanup()
            error_text = reason if reason != "hangup" else None
            if errors:
                error_text = (error_text + "; " if error_text else "") + "call cleanup error"
            result = await self.coordinator.transition(CallState.ended, error_text)
            await self._record(result)
            return result

    async def telegram_disconnected(self) -> Optional[CallRecord]:
        return await self.hangup(reason="Telegram call disconnected")

    async def cellular_disconnected(self) -> Optional[CallRecord]:
        return await self.hangup(reason="cellular call disconnected")

    async def current(self) -> Optional[CallRecord]:
        return await self.coordinator.current()

    async def _activate_if_ready(self) -> CallRecord:
        await self._require_active()
        if self._telegram_connected and self._cellular_connected:
            await self._start_audio_if_needed()
            self._cancel_timeout()
            result = await self.coordinator.transition(CallState.active)
            await self._record(result)
            return result
        result = await self.coordinator.transition(CallState.waiting_cellular)
        await self._record(result)
        return result

    async def _start_audio_if_needed(self) -> None:
        if self._audio_started:
            return
        await self.audio_start(self.telegram_handle)
        self._audio_started = True

    async def _cleanup(self) -> list[Exception]:
        self._cancel_timeout()
        errors = []
        callbacks = [(self.audio_stop, ())]
        if self._cellular_started:
            callbacks.append((self.cellular_hangup, ()))
        if self.telegram_handle is not None:
            callbacks.append((self.telegram_hangup, (self.telegram_handle,)))
        for callback, args in callbacks:
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # cleanup must continue across both legs
                errors.append(exc)
        self.telegram_handle = None
        self._telegram_connected = False
        self._cellular_connected = False
        self._audio_started = False
        self._cellular_started = False
        return errors

    async def _fail_start(self, record: CallRecord, error: Exception) -> None:
        await self._cleanup()
        result = await self.coordinator.transition(CallState.failed, type(error).__name__)
        await self._record(result)

    async def _record(self, record: CallRecord) -> None:
        if self.record_sink is not None:
            await self.record_sink(record)

    async def _require_active(self, optional: bool = False) -> Optional[CallRecord]:
        record = await self.coordinator.current()
        if record is None and not optional:
            raise CallBridgeError("no active call")
        return record

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
