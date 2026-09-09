from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from qdc507_gateway.calls.core import CallBridgeError, CallCoordinator, public_call_error
from qdc507_gateway.models import CallDirection, CallRecord, CallState, utc_now


class ClientCallController:
    """Cellular calls controlled by app or browser clients, independent of Telegram."""

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
        self._socket_reserved = False
        self._audio_attached = False
        self._cellular_started = False
        self._cellular_connected = False
        self._timeout_task: Optional[asyncio.Task] = None
        self._operation_lock = asyncio.Lock()
        self._hangup_lock = asyncio.Lock()
        self._call_end_id: Optional[str] = None
        self._call_end_event: Optional[asyncio.Event] = None

    async def start_outbound(self, number: str, installation_id: Optional[str] = None) -> CallRecord:
        async with self._operation_lock:
            record = await self.coordinator.start_outbound(
                number,
                None,
                frontend="app" if installation_id else "web",
                initial_state=CallState.waiting_client,
            )
            self._begin_call(record.id)
            self._reset_flags()
            record.owner_installation_id = installation_id
            record.expires_at = utc_now() + timedelta(seconds=self.timeout_seconds)
            await self._record(record)
            self._arm_timeout(record.id)
            return record

    async def start_inbound(self, number: Optional[str], *, frontend: str = "web") -> CallRecord:
        async with self._operation_lock:
            current = await self.coordinator.current()
            if (
                current is not None
                and current.frontend == frontend
                and current.direction == CallDirection.inbound_cellular
            ):
                if current.cellular_number is None and number is not None:
                    current.cellular_number = number
                    await self._record(current)
                return current
            record = await self.coordinator.start_inbound(
                number,
                None,
                frontend=frontend,
                initial_state=CallState.ringing_cellular,
            )
            self._begin_call(record.id)
            self._reset_flags()
            self._cellular_started = True
            record.expires_at = utc_now() + timedelta(seconds=self.timeout_seconds)
            await self._record(record)
            self._arm_timeout(record.id)
            return record

    async def require_call(self, call_id: str) -> CallRecord:
        record = await self.coordinator.current()
        if record is None or record.id != call_id or record.frontend not in {"web", "app"}:
            raise CallBridgeError("web call id does not match the active call")
        if record.expires_at is not None and record.expires_at <= utc_now():
            raise CallBridgeError("call has expired")
        return record

    async def wait_ended(self, call_id: str) -> None:
        """Wait until the named web call no longer owns the shared audio bridge."""
        if self._call_end_id != call_id or self._call_end_event is None:
            return
        event = self._call_end_event
        await event.wait()

    async def require_owner(self, call_id: str, installation_id: Optional[str]):
        record = await self.require_call(call_id)
        if record.frontend == "app" and (not installation_id or record.owner_installation_id != installation_id):
            raise CallBridgeError("call is not owned by this installation")
        return record

    async def reserve_audio(self, call_id: str, installation_id: Optional[str]):
        async with self._operation_lock:
            await self.require_owner(call_id, installation_id)
            if self._socket_reserved or self._audio_attached:
                raise CallBridgeError("call audio is already attached")
            self._socket_reserved = True

    async def attach_audio(self, call_id: str, installation_id: Optional[str] = None) -> CallRecord:
        async with self._operation_lock:
            record = await self.require_owner(call_id, installation_id)
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
                elif record.frontend == "app":
                    await self.cellular_answer()
                    self._cellular_connected = True
                    self._cancel_timeout()
                    record.expires_at = None
                    record = await self.coordinator.transition(CallState.active)
                    await self._record(record)
                return record
            except Exception as exc:
                await self._fail_unlocked(exc)
                raise

    async def answer(self, call_id: str, installation_id: Optional[str] = None) -> CallRecord:
        async with self._operation_lock:
            record = await self.require_call(call_id)
            if record.direction != CallDirection.inbound_cellular:
                raise CallBridgeError("only an incoming cellular call can be answered")
            if record.frontend == "app":
                if not installation_id:
                    raise CallBridgeError("installation_id is required")
                if record.owner_installation_id is not None:
                    await self.require_owner(call_id, installation_id)
                    return record
                record.owner_installation_id = installation_id
                record.expires_at = utc_now() + timedelta(seconds=15)
                record = await self.coordinator.transition(CallState.waiting_client)
                await self._record(record)
                self._arm_timeout(call_id, seconds=15)
                return record
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
            if record is None or record.frontend not in {"web", "app"}:
                return record
            self._cellular_connected = True
            if not self._audio_attached:
                return record
            self._cancel_timeout()
            record.expires_at = None
            record = await self.coordinator.transition(CallState.active)
            await self._record(record)
            return record

    async def cellular_disconnected(self) -> Optional[CallRecord]:
        return await self.hangup(reason="cellular call disconnected")

    async def websocket_disconnected(self, call_id: str) -> Optional[CallRecord]:
        record = await self.coordinator.current()
        if record is None or record.id != call_id or record.frontend not in {"web", "app"}:
            return record
        return await self.hangup(call_id, reason="web audio disconnected")

    async def hangup(
        self,
        call_id: Optional[str] = None,
        reason: str = "hangup",
        *, installation_id: Optional[str] = None, client: bool = False,
    ) -> Optional[CallRecord]:
        async with self._hangup_lock:
            async with self._operation_lock:
                record = await self.coordinator.current()
                if record is None:
                    return None
                if record.frontend not in {"web", "app"}:
                    raise CallBridgeError("active call is not controlled by the web frontend")
                if call_id is not None and record.id != call_id:
                    raise CallBridgeError("call id does not match the active call")
                if client and record.frontend == "app":
                    if not installation_id:
                        raise CallBridgeError("installation_id is required")
                    if record.owner_installation_id is not None:
                        await self.require_owner(record.id, installation_id)
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
        self._socket_reserved = False
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

    def _arm_timeout(self, call_id: str, seconds: Optional[float] = None) -> None:
        self._cancel_timeout()
        self._timeout_task = asyncio.create_task(self._timeout(call_id, seconds))

    def _cancel_timeout(self) -> None:
        task = self._timeout_task
        self._timeout_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _timeout(self, call_id: str, seconds: Optional[float] = None) -> None:
        try:
            await asyncio.sleep(self.timeout_seconds if seconds is None else seconds)
            await self.hangup(call_id, reason="call timeout")
        except asyncio.CancelledError:
            return
        except CallBridgeError:
            return

