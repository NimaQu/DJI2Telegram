from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable, Set

from qdc507_gateway.models import GatewayEvent


logger = logging.getLogger("qdc507_gateway.events")


class EventBus:
    def __init__(self, persist: Callable[[GatewayEvent], Awaitable[None]] | None = None):
        self._subscribers: Set[asyncio.Queue] = set()
        self._persist = persist
        self._persist_lock = asyncio.Lock()

    async def publish(self, event: GatewayEvent) -> None:
        payload = json.dumps(event.payload, ensure_ascii=False, default=str)
        if event.type.endswith("_error") or event.type.endswith(".error"):
            logger.warning("event=%s payload=%s", event.type, payload)
        else:
            logger.info("event=%s payload=%s", event.type, payload)
        if self._persist is not None:
            async with self._persist_lock:
                await self._persist(event)
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            # The queue is bounded deliberately.  A slow SSE client must not
            # block AT/URC handling or a call-state transition.
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[GatewayEvent]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
