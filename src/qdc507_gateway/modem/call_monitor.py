from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from qdc507_gateway.models import CallRecord, CallState, GatewayEvent


async def monitor_cellular_call_status(
    current_call: Callable[[], Awaitable[Optional[CallRecord]]],
    read_status: Callable[[], Awaitable[dict[str, Any]]],
    on_connected: Callable[[], Awaitable[Any]],
    publish_event: Callable[[GatewayEvent], Awaitable[Any]],
    *,
    poll_seconds: float = 1.0,
) -> None:
    """Poll read-only modem state while a call waits for cellular answer."""
    if poll_seconds <= 0:
        raise ValueError("call status polling interval must be positive")
    last_marker: Optional[tuple[Any, ...]] = None
    while True:
        call_id: Optional[str] = None
        try:
            record = await current_call()
            if record is None or record.state != CallState.waiting_cellular:
                last_marker = None
            else:
                call_id = record.id
                status = await read_status()
                voice_calls = status.get("voice_calls") or []
                marker = (
                    call_id,
                    status.get("state"),
                    status.get("source"),
                    tuple(call.get("status_code") for call in voice_calls),
                )
                if marker != last_marker:
                    last_marker = marker
                    await publish_event(GatewayEvent("call.cellular.status", {
                        "call_id": call_id,
                        "state": status.get("state", "unknown"),
                        "source": status.get("source", "unknown"),
                        "voice_calls": len(voice_calls),
                    }))
                if status.get("state") == "active":
                    await on_connected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            marker = (call_id, "error", type(exc).__name__)
            if marker != last_marker:
                last_marker = marker
                await publish_event(GatewayEvent("call.cellular.status_error", {
                    "call_id": call_id,
                    "error": type(exc).__name__,
                }))
        await asyncio.sleep(poll_seconds)
