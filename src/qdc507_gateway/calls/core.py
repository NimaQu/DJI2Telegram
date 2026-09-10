from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Optional

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
        *,
        frontend: str = "web",
        initial_state: CallState = CallState.waiting_client,
    ) -> CallRecord:
        async with self.lock:
            if self.active and self.active.state not in (CallState.ended, CallState.failed):
                raise CallBusyError("another call is already active")
            self.active = CallRecord(
                id=str(uuid.uuid4()), direction=CallDirection.outbound_cellular,
                state=initial_state, cellular_number=number,
                frontend=frontend,
            )
            return self.active

    async def start_inbound(
        self,
        number: Optional[str],
        *,
        frontend: str = "web",
        initial_state: CallState = CallState.waiting_client,
    ) -> CallRecord:
        async with self.lock:
            if self.active and self.active.state not in (CallState.ended, CallState.failed):
                raise CallBusyError("another call is already active")
            self.active = CallRecord(
                id=str(uuid.uuid4()), direction=CallDirection.inbound_cellular,
                state=initial_state, cellular_number=number,
                frontend=frontend,
            )
            return self.active

    async def transition(self, state: CallState, error: Optional[str] = None) -> CallRecord:
        async with self.lock:
            if self.active is None:
                raise RuntimeError("no active call")
            self.active.state = state
            self.active.last_error = error
            if state in (CallState.active, CallState.ended, CallState.failed):
                self.active.expires_at = None
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

